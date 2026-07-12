#!/usr/bin/env python3
"""Soltop — read GPU utilization and CPU cluster residency via IOReport.

Computes GPU/CPU active utilization from P-State residency on Apple Silicon.
"""
import ctypes
import ctypes.util
import re
import subprocess
import time
from collections import deque

__version__ = "0.7.2"

from ctypes import (
    c_void_p,
    c_char_p,
    c_int,
    c_uint32,
    c_uint64,
    c_int64,
    c_long,
    POINTER,
    byref,
    create_string_buffer,
)

CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
IOR = ctypes.CDLL("/usr/lib/libIOReport.dylib")
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

kCFStringEncodingUTF8 = 0x08000100

# --- CoreFoundation ---------------------------------------------------------
CF.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
CF.CFStringCreateWithCString.restype = c_void_p
CF.CFRelease.argtypes = [c_void_p]
CF.CFRelease.restype = None

CF.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_int64, c_uint32]
CF.CFStringGetCString.restype = ctypes.c_bool

CF.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
CF.CFDictionaryGetValue.restype = c_void_p

# Enumerating a node's properties (used to discover voltage-states* keys, whose
# numbering is chip-specific and so cannot be looked up by name).
CF.CFDictionaryGetCount.argtypes = [c_void_p]
CF.CFDictionaryGetCount.restype = c_long
CF.CFDictionaryGetKeysAndValues.argtypes = [c_void_p, c_void_p, c_void_p]
CF.CFDictionaryGetKeysAndValues.restype = None
CF.CFGetTypeID.argtypes = [c_void_p]
CF.CFGetTypeID.restype = c_long
CF.CFDataGetTypeID.restype = c_long

CF.CFArrayGetCount.argtypes = [c_void_p]
CF.CFArrayGetCount.restype = c_int64
CF.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_int64]
CF.CFArrayGetValueAtIndex.restype = c_void_p

# --- IOReport ---------------------------------------------------------------
IOR.IOReportCopyAllChannels.argtypes = [c_uint64, c_uint64]
IOR.IOReportCopyAllChannels.restype = c_void_p

IOR.IOReportCopyChannelsInGroup.argtypes = [c_void_p, c_void_p, c_uint64, c_uint64, c_uint64]
IOR.IOReportCopyChannelsInGroup.restype = c_void_p

IOR.IOReportMergeChannels.argtypes = [c_void_p, c_void_p, c_void_p]
IOR.IOReportMergeChannels.restype = None

IOR.IOReportCreateSubscription.argtypes = [c_void_p, c_void_p, POINTER(c_void_p), c_uint64, c_void_p]
IOR.IOReportCreateSubscription.restype = c_void_p

IOR.IOReportCreateSamples.argtypes = [c_void_p, c_void_p, c_void_p]
IOR.IOReportCreateSamples.restype = c_void_p

IOR.IOReportCreateSamplesDelta.argtypes = [c_void_p, c_void_p, c_void_p]
IOR.IOReportCreateSamplesDelta.restype = c_void_p

# Per-channel (dictionary) accessors
IOR.IOReportChannelGetGroup.argtypes = [c_void_p]
IOR.IOReportChannelGetGroup.restype = c_void_p
IOR.IOReportChannelGetSubGroup.argtypes = [c_void_p]
IOR.IOReportChannelGetSubGroup.restype = c_void_p
IOR.IOReportChannelGetChannelName.argtypes = [c_void_p]
IOR.IOReportChannelGetChannelName.restype = c_void_p

IOR.IOReportStateGetCount.argtypes = [c_void_p]
IOR.IOReportStateGetCount.restype = c_int
IOR.IOReportStateGetNameForIndex.argtypes = [c_void_p, c_int]
IOR.IOReportStateGetNameForIndex.restype = c_void_p
IOR.IOReportStateGetResidency.argtypes = [c_void_p, c_int]
IOR.IOReportStateGetResidency.restype = c_int64

IOR.IOReportSimpleGetIntegerValue.argtypes = [c_void_p, c_int]
IOR.IOReportSimpleGetIntegerValue.restype = c_int64


def cfstr(s):
    return CF.CFStringCreateWithCString(None, s.encode(), kCFStringEncodingUTF8)


def from_cfstr(ref):
    if not ref:
        return None
    buf = create_string_buffer(512)
    ok = CF.CFStringGetCString(ref, buf, len(buf), kCFStringEncodingUTF8)
    if not ok:
        return None
    return buf.value.decode("utf-8", "replace")


def copy_group(group, subgroup=None):
    g = cfstr(group)
    sg = cfstr(subgroup) if subgroup else None

    ch = IOR.IOReportCopyChannelsInGroup(g, sg, 0, 0, 0)

    CF.CFRelease(g)
    if sg:
        CF.CFRelease(sg)

    return ch


def iter_channels(delta):
    """Iterate each channel dictionary inside a sample/delta dictionary."""
    key = cfstr("IOReportChannels")
    arr = CF.CFDictionaryGetValue(delta, key)
    CF.CFRelease(key)
    if not arr:
        return
    n = CF.CFArrayGetCount(arr)
    for i in range(n):
        yield CF.CFArrayGetValueAtIndex(arr, i)


def read_states(chan):
    """Return the channel's P-State residency as {state_name: residency}."""
    count = IOR.IOReportStateGetCount(chan)
    states = {}
    for i in range(count):
        name = from_cfstr(IOR.IOReportStateGetNameForIndex(chan, i))
        res = IOR.IOReportStateGetResidency(chan, i)
        states[name or f"P{i}"] = res
    return states


# State-name keywords treated as idle (inactive). If Apple renames states,
# this is the only place to adjust.
# Note: powermetrics emits the firmware-computed active residency directly, but
# the public API does not expose it, so we approximate it as "total - idle".
# Every state not matching a keyword below is treated as active, so if no idle
# state matches at all, active_ratio could report 100% (handled via None below).
IDLE_NAMES = ("IDLE", "OFF", "DOWN", "PAUSE", "SLEEP")


def is_idle_state(name):
    n = (name or "").upper()
    return any(k in n for k in IDLE_NAMES)


def active_ratio(states):
    """Active residency ratio (0..1), excluding idle-family states.

    If no idle state can be identified (possible naming-scheme change), the
    result is unreliable, so return None and let the caller treat it as 0.
    """
    total = sum(states.values())
    if total <= 0:
        return 0.0
    idle_states = [res for name, res in states.items() if is_idle_state(name)]
    if not idle_states and len(states) > 1:
        # Multiple states but none matched idle -> suspect a naming change.
        return None
    idle = sum(idle_states)
    return max(0.0, (total - idle) / total)


def classify_group(group):
    """Classify a group name as gpu/cpu (tolerant of case/generation changes)."""
    u = (group or "").upper()
    if "GPU" in u:
        return "gpu"
    if "CPU" in u:
        return "cpu"
    return None


# The subgroups that actually carry utilization residency, in preference order.
# This must NOT be a loose "anything with STATE in the name" match: 'GPU Stats'
# alone also exposes Fender State, UV Warn State, CLTM-induced GPU Performance
# States and the AFR/Boost *controller* states, which are latched status
# registers that sit pinned at 100%. Averaging those in reported ~40% GPU on a
# fully idle machine. 'GPU Performance States' (GPUPH) is the channel
# powermetrics reports as GPU active residency.
_UTIL_SUBGROUPS = {
    "gpu": ("GPU Performance States",),
    "cpu": ("CPU Core Performance States",),
}
# Substrings identifying subgroups that are NOT utilization, used only by the
# fallback scan below if Apple renames the canonical subgroups above.
_NOT_UTIL = ("CONTROLLER", "CLTM", "FENDER", "WARN", "DVD", "REASON CODE",
             "HISTOGRAM", "THROTTLER", "COMPLEX", "VOLTAGE")


def _fallback_state_subgroups(seen, kind):
    """Rename-tolerant scan for one kind ('gpu'/'cpu'), from already-seen pairs.

    Keeps the original 'works even if Apple renames things' property, but
    excludes the status-register subgroups that are not utilization.

    Returns at most ONE subgroup. Several can match -- this M4 Pro exposes GPU
    Performance States, AFR Performance States and GPU Software Performance
    States -- and subscribing to all of them would average them into a single
    figure, which is exactly the bug that made an idle GPU read ~40%.

    Rank the candidates: the name must lead with the unit itself ("GPU ..." for
    gpu, "CPU"/"ECPU"/"PCPU ..." for cpu), which drops AFR (the display refresh
    channel, not GPU compute); then prefer the shortest name, so the plain
    residency channel beats a qualified variant like "GPU Software ...".
    """
    prefixes = ("GPU",) if kind == "gpu" else ("CPU", "ECPU", "PCPU")
    wanted = []
    for group, subgroup in seen:
        if classify_group(group) != kind:
            continue
        u = subgroup.upper()
        if "PERFORMANCE STATE" not in u or any(b in u for b in _NOT_UTIL):
            continue
        wanted.append((group, subgroup))
    if not wanted:
        return []
    leading = [p for p in wanted if p[1].upper().startswith(prefixes)] or wanted
    return [min(leading, key=lambda p: (len(p[1].split()), p[1]))]


def discover_state_channels():
    """Discover the GPU/CPU utilization residency subgroups and merge them.

    Prefers the known utilization subgroups (_UTIL_SUBGROUPS) and falls back to
    a filtered scan if they are absent, so a rename still degrades gracefully.
    Selecting here rather than averaging everything matters twice over: it is
    what makes the reported percentages correct, and copying a channel group
    costs ~0.1s each, so taking 2 instead of 31 also cuts startup from ~3.2s to
    ~0.2s.
    """
    all_ch = IOR.IOReportCopyAllChannels(0, 0)
    if not all_ch:
        raise RuntimeError("IOReportCopyAllChannels failed")

    # Keep discovery order stable: a set would let the merge order (and hence the
    # order channels appear in the UI) vary between runs.
    seen, available = [], []
    for chan in iter_channels(all_ch):
        group = from_cfstr(IOR.IOReportChannelGetGroup(chan))
        subgroup = from_cfstr(IOR.IOReportChannelGetSubGroup(chan))
        pair = (group, subgroup)
        if classify_group(group) and subgroup and pair not in seen:
            seen.append(pair)
    CF.CFRelease(all_ch)

    # Fall back PER KIND, not globally: if only one of the two canonical names is
    # renamed, a global "is `available` empty?" check would find the surviving one,
    # skip the fallback entirely, and drop that whole subsystem from the display.
    for kind, names in _UTIL_SUBGROUPS.items():
        found = [(g, sg) for g, sg in seen
                 if classify_group(g) == kind and sg in names]
        if not found:
            found = _fallback_state_subgroups(seen, kind)
        available.extend(found)

    if not available:
        raise RuntimeError("no GPU/CPU state channels found")

    base = None
    for group, subgroup in available:
        ch = copy_group(group, subgroup)
        if not ch:
            continue
        if base is None:
            base = ch
        else:
            IOR.IOReportMergeChannels(base, ch, None)
            CF.CFRelease(ch)
    if base is None:
        raise RuntimeError("no channels copied")
    return base


# Energy Model channels of interest -> display label. Their delta over the
# interval is energy consumed, so power (mW) = delta / interval_seconds.
# "SoC" is derived as the sum of these components.
ENERGY_KEYS = {"CPU Energy": "CPU", "GPU": "GPU", "ANE": "ANE", "DRAM": "DRAM"}
POWER_LABELS = ("CPU", "GPU", "ANE", "DRAM")


# --- DVFS frequency tables (voltage-states in IORegistry, no sudo) -----------
# The GPU table holds plain Hz. The CPU tables hold the *period* of each step,
# not its frequency, which is why they descend while the GPU's ascends:
#
#     MHz = CPU_PERIOD_NUMERATOR / raw
#
# Verified against `sudo powermetrics --samplers cpu_power` on an M4 Pro: every
# step of both ladders it prints is reproduced exactly --
#   E: 1020 1404 1788 2112 2352 2532 2592
#   P: 1260 1512 1800 2088 2352 2616 2868 3096 3300 3468 3624 3756 3852 3924
#      3996 4044 4104 4416 4512
# The two clusters share one numerator, so this is not a per-cluster fudge
# factor. (Earlier versions normalised the raw ladder against a hardcoded
# per-cluster max instead, which produced confidently wrong clocks on any chip
# that did not match; that is what this replaces.)
#
# The numerator carries across generations: on an M5 Pro the same constant turns
# voltage-states5 into the S-cluster's 1308..4608 ladder and voltage-states22/23
# into the P0/P1 1344..4380 ladder, each matching powermetrics exactly.
CPU_PERIOD_NUMERATOR = 65_532_288

# The *numbering* of the voltage-states keys does NOT carry across generations,
# so nothing here may hardcode it. On an M4 Pro the CPU ladders live in
# voltage-states1 (E) and voltage-states5 (P); on an M5 Pro there is no E
# cluster at all, voltage-states5 holds the S ladder, and P0/P1 live in
# voltage-states22/23. Binding a cluster to a fixed key name therefore reads the
# wrong ladder on the next chip -- confidently, and with no symptom other than a
# wrong number. Instead, discover every voltage-states* table and bind each
# cluster to one by matching the ladder LENGTH against the cluster's P-state
# count, which IOReport reports per core (see load_dvfs/match_cpu_ladder).
_VOLTAGE_STATE_RE = re.compile(r"^voltage-states(\d+)(-sram)?$")

# Plausibility bounds. A derived ladder outside these is not a CPU/GPU DVFS
# table (or the encoding changed), and printing a clock from it would be a
# fabrication -- so the caller shows no MHz instead.
_SANE_MHZ = (100.0, 10_000.0)

# An Apple GPU clocks far below its CPU (an M4 Pro tops out at 1578 MHz, an M5
# Pro at 1620). Several unrelated Hz tables sit alongside it in the IORegistry
# -- 801..2004 and 732..2472 on an M5 -- so a ceiling is what tells them apart.
# Generous enough to allow for headroom on future parts.
_GPU_MAX_MHZ = 1900.0


def _read_voltage_state_tables():
    """Return {keyname: [uint32, ...]} for every voltage-states* CFData property.

    Enumerates each IOService node's properties rather than looking up a fixed
    set of key names, because the key numbering varies by chip and the names we
    want are therefore not known in advance (an M5 Pro has voltage-states22/23
    where an M4 Pro has none). Walks the IOService plane once -- the traversal
    dominates the cost.
    """
    CF.CFDataGetLength.restype = c_long
    CF.CFDataGetLength.argtypes = [c_void_p]
    CF.CFDataGetBytePtr.restype = c_void_p
    CF.CFDataGetBytePtr.argtypes = [c_void_p]
    data_type = CF.CFDataGetTypeID()
    it = c_uint32(0)
    if IOKIT.IORegistryCreateIterator(0, b"IOService", kIORegistryIterateRecursively, byref(it)) != 0:
        return {}
    out = {}
    try:
        while True:
            o = IOKIT.IOIteratorNext(it)
            if not o:
                break
            props = c_void_p()
            if IOKIT.IORegistryEntryCreateCFProperties(o, byref(props), None, 0) == 0 and props.value:
                n = CF.CFDictionaryGetCount(props)
                if n > 0:
                    keys = (c_void_p * n)()
                    vals = (c_void_p * n)()
                    CF.CFDictionaryGetKeysAndValues(props, keys, vals)
                    for k, v in zip(keys, vals):
                        name = from_cfstr(k)
                        if not name or not _VOLTAGE_STATE_RE.match(name) or name in out:
                            continue
                        if not v or CF.CFGetTypeID(v) != data_type:
                            continue
                        nbytes = CF.CFDataGetLength(v)
                        if nbytes >= 4:
                            # from_address copies into a Python list before props
                            # is released, so the bytes need not outlive this call.
                            out[name] = list((c_uint32 * (nbytes // 4)).from_address(
                                CF.CFDataGetBytePtr(v)))
                CF.CFRelease(props)
            IOKIT.IOObjectRelease(o)
    finally:
        IOKIT.IOObjectRelease(it)
    return out


def _decode_ladder(vals):
    """Turn one table's raw first-of-pair uints into ascending MHz, or None.

    The encoding is identified from the values themselves rather than from the
    key name, because the key numbering is not stable across chips:

      * Hz     -- large absolute values (GPU tables, and the '-sram' twins of
                  the CPU tables, which carry kHz).
      * period -- small values, converted via CPU_PERIOD_NUMERATOR.

    Returns None when neither reading lands in a plausible MHz range, which is
    how a table we do not actually understand gets rejected instead of printed.
    """
    if not vals:
        return None
    lo, hi = _SANE_MHZ
    # Three encodings, and the kind must come from the ENCODING, not merely from
    # landing in a sane MHz range:
    #
    #   Hz     (>= 1e8)  -- a GPU table, the only one stored as a true frequency
    #   kHz    (~1e6)    -- a '-sram' twin of a CPU table, NOT a GPU table
    #   period (small)   -- a CPU table (MHz = CPU_PERIOD_NUMERATOR / raw)
    #
    # Lumping Hz and kHz together as one "absolute" kind is what let a CPU
    # ladder be handed to the GPU: on an M5 Pro, voltage-states22-sram decodes
    # from kHz to 1344..4380 MHz, and the GPU bound to it and reported 1644 MHz
    # against a real ladder that tops out at 1620.
    for kind, mhz in (("gpu", [v / 1e6 for v in vals] if min(vals) >= 1e8 else None),
                      ("sram", [v / 1e3 for v in vals]),
                      ("period", [CPU_PERIOD_NUMERATOR / v for v in vals])):
        if mhz is None:
            continue
        ladder = sorted(mhz)
        if lo <= ladder[0] and ladder[-1] <= hi:
            return (kind, ladder)
    return None


def load_dvfs():
    """Return {key: (kind, [ascending MHz])} for every table we can decode.

    Keyed by the raw IORegistry key name ('voltage-states5', ...), NOT by
    cluster: which table belongs to which cluster is decided later, by matching
    ladder length against the cluster's P-state count (see match_cpu_ladder). The
    numbering differs per chip, so this layer must not presume a mapping.
    ``kind`` is 'period' for CPU tables and 'absolute' for GPU ones.
    """
    try:
        found = _read_voltage_state_tables()
    except Exception:
        return {}
    tables = {}
    for key, raw in found.items():
        vals = [f for f in raw[0::2] if f]   # first uint of each (freq, volt) pair
        decoded = _decode_ladder(vals)
        if decoded:
            tables[key] = decoded
    return tables


def _rank_key(item):
    """Order tables by key number, so the choice is stable, not dict-ordered."""
    key, _ = item
    m = _VOLTAGE_STATE_RE.match(key)
    return int(m.group(1)) if m else 1 << 30


def match_cpu_ladder(nsteps, tables):
    """Pick the CPU ladder for a cluster with ``nsteps`` P-states, or [].

    IOReport names each core's P-states V0P18, V1P17, ... so the number of
    non-idle states IS the length of that cluster's ladder, and a CPU table with
    exactly that many entries is the cluster's.

    Verified on an M5 Pro, where the key numbering is nothing like the M4's:
    the S-cluster (20 steps) binds to voltage-states5, P0/P1 (15 steps each) to
    voltage-states22/23 -- and the E-cluster key the old code hardcoded does not
    exist on that chip.

    P0 and P1 have identical ladders (they differ only in voltage), so both
    resolve to the same frequencies. That is correct: powermetrics prints the
    same ladder for both.
    """
    if not nsteps:
        return []
    for _, (kind, ladder) in sorted(tables.items(), key=_rank_key):
        if kind == "period" and len(ladder) == nsteps:
            return ladder
    return []


def match_gpu_ladder(tables, cap_mhz=_GPU_MAX_MHZ):
    """Pick the GPU ladder, or [].

    NOT chosen by step count. The GPU's IOReport states are a fixed P1..P15 set
    on both an M4 Pro and an M5 Pro, while the real ladder has 15 entries on an
    M4 and 13 on an M5 -- so the state count is not the ladder length and must
    not be matched against one. (Doing so bound the M5's GPU to a 15-step *CPU*
    table and reported 1644 MHz, above the 1620 MHz top of its real ladder.)

    Chosen instead by the two properties that actually identify a GPU table:

      * it is stored as true Hz -- the CPU tables store a period, and their
        '-sram' twins store kHz, which is what leaked a CPU ladder to the GPU;
      * an Apple GPU clocks far below its CPU, so a ladder topping out above
        cap_mhz is some other unit's table (an M5 Pro exposes several Hz tables:
        the GPU's 338..1620, plus 801..2004 and 732..2472 which are not).

    Among the candidates the lowest key number wins -- voltage-states9 on both
    chips we have data for.
    """
    for _, (kind, ladder) in sorted(tables.items(), key=_rank_key):
        if kind == "gpu" and ladder[-1] <= cap_mhz:
            return ladder
    return []


_VP_RE = re.compile(r"^V(\d+)P(\d+)$")   # CPU: V ascends with the step, P descends
_P_RE = re.compile(r".*?P(\d+)$")        # GPU / fallback: plain ascending suffix


def _pstate_index(name):
    """DVFS ladder index (ascending: 0 = slowest) from a state name like 'V18P0'.

    CPU state names are 'V<v>P<p>', where the two counters run in OPPOSITE
    directions: v ascends with the voltage/frequency step while p descends, so
    v + p == len(ladder) - 1 for every state in a cluster (e.g. the 19-step P
    ladder yields V0P18, V1P17, ... V18P0). Reading the P suffix as the ladder
    index therefore inverts the ladder -- V18P0, the TOP step, parses as 0 and
    lands on the ladder floor -- which reported a pegged CPU at its minimum
    clock. The V field is the ascending index, so use that.

    GPU state names have no V field ('P3', 'GPUPH'); fall back to the numeric
    suffix there, which is already ascending.
    """
    if not name:
        return None
    m = _VP_RE.match(name)
    if m:
        return int(m.group(1))
    m = _P_RE.match(name)
    if m:
        return int(m.group(1))
    return None


def _nsteps(cores):
    """Number of DVFS steps a cluster exposes = its non-idle P-state count.

    IOReport names every state a core can occupy (DOWN, IDLE, V0P18, V1P17...),
    so dropping the idle-family names leaves exactly one name per ladder rung.
    That count is what binds the cluster to its voltage-states table.

    Takes the widest count across the cluster's cores rather than the first: a
    short count would bind a shorter ladder and report a plausible wrong clock,
    whereas the widest is the ladder the hardware actually exposes.
    """
    best = 0
    for core in cores:
        states = core.get("states") or {}
        n = sum(1 for name in states if not is_idle_state(name))
        best = max(best, n)
    return best


def cluster_freq_mhz(cores, table):
    """Active-residency-weighted clock, i.e. powermetrics' "HW active frequency".

    Idle residency is EXCLUDED, not counted at the bottom of the ladder. Checked
    against `sudo powermetrics` on an M4 Pro: for an E-cluster at 73.21% active
    residency it prints "HW active frequency: 1920 MHz", and weighting only the
    active states reproduces that (1916 MHz), while folding idle in at the ladder
    floor gives 1678 MHz. So this is the number powermetrics means -- the clock a
    core runs at while it is actually running, not the mean over wall time.

    ``table`` is either a plain ascending list of values or the {"values","unit"}
    dict produced by load_dvfs(). Returns the weighted value (0.0 if unknown);
    Returns MHz, or 0.0 when the ladder or the residency is unknown.
    """
    values = table.get("values") if isinstance(table, dict) else table
    if not values:
        return 0.0
    num = den = 0.0
    for core in cores:
        for name, res in core.get("states", {}).items():
            if res <= 0 or is_idle_state(name):
                continue
            idx = _pstate_index(name)
            if idx is None:
                continue
            idx = max(0, min(len(values) - 1, idx))
            num += res * values[idx]
            den += res
    return (num / den) if den > 0 else 0.0


# Populated lazily on first Sampler (needs IOKIT, defined further below).
DVFS = None


def build_subscription():
    chans = discover_state_channels()
    # Also subscribe to the Energy Model group for CPU/GPU/ANE power (no sudo).
    energy = copy_group("Energy Model", None)
    if energy:
        IOR.IOReportMergeChannels(chans, energy, None)
        CF.CFRelease(energy)
    sub = c_void_p()
    subscribed = IOR.IOReportCreateSubscription(None, chans, byref(sub), 0, None)
    if not sub.value:
        CF.CFRelease(chans)
        raise RuntimeError("subscription failed")
    return subscribed, chans, sub


class Sampler:
    """Create the subscription once and repeatedly read deltas."""

    def __init__(self):
        global DVFS
        if DVFS is None:
            try:
                DVFS = load_dvfs()
            except Exception:
                DVFS = {}
        self.closed = False
        self.subscribed, self.chans, self.sub = build_subscription()
        self.prev = IOR.IOReportCreateSamples(self.subscribed, self.chans, None)
        self.prev_time = time.monotonic()
        # Retain channel metadata across samples; missing/parked channels have
        # their measurements reset to zero in read().
        self.last = {}          # key -> most recent entry
        self.order = {}         # group -> [key, ...] preserve observed order
        self.power = {}         # label -> power in mW (from Energy Model)
        self.power_peak = {}    # label -> true peak mW observed (for display)
        self.power_sum = {}     # label -> cumulative mW (for average)
        self.power_cnt = 0      # number of samples accumulated

    def _release(self):
        for ref in (self.prev, self.subscribed, self.chans, self.sub):
            if ref:
                try:
                    CF.CFRelease(ref)
                except Exception:
                    pass
        self.prev = self.subscribed = self.chans = self.sub = None

    def close(self):
        """Release the native subscription and sample objects."""
        self._release()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _recreate(self):
        """Release resources and re-subscribe when the subscription drops.

        If re-subscribing fails, close the Sampler for good rather than leaving
        it with every pointer NULL but closed=False. In that zombie state the
        next read() would sail past the closed guard, see `not self.prev`, take
        this same recovery path and silently succeed -- resurrecting an object
        the caller was told had failed.
        """
        self._release()
        try:
            self.subscribed, self.chans, self.sub = build_subscription()
            self.prev = IOR.IOReportCreateSamples(self.subscribed, self.chans, None)
        except BaseException:
            self.close()
            raise
        self.prev_time = time.monotonic()

    def read(self, interval=1.0):
        # Without this, the dropped-subscription recovery below cannot tell "the
        # subscription died" from "the caller closed this sampler", so a read()
        # after close() would silently re-subscribe and leak a native
        # subscription nobody owns -- making close() a lie.
        if self.closed:
            raise RuntimeError("Sampler is closed")
        time.sleep(interval)

        cur = IOR.IOReportCreateSamples(self.subscribed, self.chans, None)
        cur_time = time.monotonic()
        delta = IOR.IOReportCreateSamplesDelta(self.prev, cur, None) if cur else None
        if not self.prev or not cur or not delta:
            # Subscription may have dropped: clean up and re-subscribe once.
            if cur:
                CF.CFRelease(cur)
            self._recreate()
            time.sleep(interval)
            cur = IOR.IOReportCreateSamples(self.subscribed, self.chans, None)
            cur_time = time.monotonic()
            delta = IOR.IOReportCreateSamplesDelta(self.prev, cur, None) if cur else None
            if not cur or not delta:
                if cur:
                    CF.CFRelease(cur)
                # Re-subscribing worked but sampling still fails: give up and
                # close, or we would leak the fresh subscription and leave a
                # sampler that says it failed yet still holds native resources.
                self.close()
                raise RuntimeError("samples failed (still failing after re-subscribe)")

        # The previous sample is no longer needed: release and swap in the new one.
        CF.CFRelease(self.prev)
        self.prev = cur
        elapsed = max(cur_time - self.prev_time, 1e-9)
        self.prev_time = cur_time

        observed = set()
        current_power = {lbl: 0.0 for lbl in POWER_LABELS}

        # Update observed channels with their real active value (0 if idle).
        for chan in iter_channels(delta):
            group = from_cfstr(IOR.IOReportChannelGetGroup(chan))
            name = from_cfstr(IOR.IOReportChannelGetChannelName(chan))

            # Energy Model: delta energy over the interval -> power (mW).
            if group == "Energy Model":
                if name in ENERGY_KEYS:
                    try:
                        energy = IOR.IOReportSimpleGetIntegerValue(chan, 0)
                        current_power[ENERGY_KEYS[name]] = max(0.0, energy / elapsed)
                    except Exception:
                        pass
                continue

            if classify_group(group) is None:
                continue
            subgroup = from_cfstr(IOR.IOReportChannelGetSubGroup(chan))
            states = read_states(chan)
            if not states:
                continue

            key = (group, subgroup, name)
            observed.add(key)
            if key not in self.order.setdefault(group, []):
                self.order[group].append(key)

            total = sum(states.values())
            ratio = active_ratio(states) if total > 0 else 0.0
            # None means the state names no longer look like anything we know, so
            # utilization is untrustworthy -> report 0% AND drop the states, or the
            # frequency would still be derived from them and show a busy clock at 0%.
            active = ratio if ratio is not None else 0.0
            if ratio is None:
                states, total = {}, 0

            self.last[key] = {
                "name": name, "group": group, "subgroup": subgroup,
                "kind": classify_group(group), "active": active, "total": total,
                "states": states,
            }

        # A state channel omitted from a delta is normally parked/inactive. Do
        # not retain a prior busy value, which would create phantom utilization.
        for key, entry in self.last.items():
            if key not in observed:
                entry.update(active=0.0, total=0, states={})

        # Values from delta are already copied into Python dicts, so release it.
        CF.CFRelease(delta)

        # Missing energy channels mean no value for this delta, not "reuse the
        # previous sample". Reset them to zero to avoid stale power readings.
        self.power = current_power
        self.power["SoC"] = sum(self.power[l] for l in POWER_LABELS)
        # Accumulate peak and running average per component, over the session.
        self.power_cnt += 1
        for lbl, mw in self.power.items():
            self.power_peak[lbl] = max(self.power_peak.get(lbl, 0.0), mw)
            self.power_sum[lbl] = self.power_sum.get(lbl, 0.0) + mw
        power_avg = {lbl: s / self.power_cnt for lbl, s in self.power_sum.items()}

        # Emit in observed order, including parked channels reset to zero.
        gpu, cpu = [], []
        for group, keys in self.order.items():
            dst = gpu if classify_group(group) == "gpu" else cpu
            for key in keys:
                if key in self.last:
                    dst.append(self.last[key])
        return {"gpu": gpu, "cpu": cpu,
                "power": dict(self.power),
                "power_avg": power_avg, "power_peak": dict(self.power_peak)}


# --- Per-process GPU usage (like nvidia-smi) ---------------------------------
# The Apple GPU driver publishes a per-client (per-process) command-queue
# accounting in the IORegistry: each AGXDeviceUserClient carries
#   IOUserClientCreator = "pid <n>, <name>"
#   AppUsage            = [ {accumulatedGPUTime: <ns>, ...}, ... ]
# Summing accumulatedGPUTime per pid and diffing over time yields per-process
# GPU busy time -- no powermetrics, and readable without sudo.
# (task_info(TASK_POWER_INFO_V2) does NOT work on Apple Silicon: task_for_pid is
#  blocked even for root, and the gpu field stays 0.)

IOKIT = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")

IOKIT.IORegistryCreateIterator.argtypes = [c_uint32, c_char_p, c_uint32, POINTER(c_uint32)]
IOKIT.IORegistryCreateIterator.restype = c_int
IOKIT.IOIteratorNext.argtypes = [c_uint32]
IOKIT.IOIteratorNext.restype = c_uint32
IOKIT.IOObjectRelease.argtypes = [c_uint32]
IOKIT.IOObjectRelease.restype = c_int
IOKIT.IOObjectConformsTo.argtypes = [c_uint32, c_char_p]
IOKIT.IOObjectConformsTo.restype = ctypes.c_bool
IOKIT.IORegistryEntryCreateCFProperties.argtypes = [c_uint32, POINTER(c_void_p), c_void_p, c_uint32]
IOKIT.IORegistryEntryCreateCFProperties.restype = c_int

CF.CFNumberGetValue.argtypes = [c_void_p, c_int64, c_void_p]
CF.CFNumberGetValue.restype = ctypes.c_bool

kCFNumberSInt64Type = 4
kIORegistryIterateRecursively = 1
# GPU client user-client class. Falls back through generations if renamed.
GPU_CLIENT_CLASSES = (b"AGXDeviceUserClient", b"IOGPUDeviceUserClient")

# Cache CFString keys once (creating them per lookup would leak a CFString each).
_K_CREATOR = cfstr("IOUserClientCreator")
_K_APPUSAGE = cfstr("AppUsage")
_K_GPUTIME = cfstr("accumulatedGPUTime")


def _cfnum_i64(ref):
    if not ref:
        return 0
    out = c_int64(0)
    if CF.CFNumberGetValue(ref, kCFNumberSInt64Type, byref(out)):
        return out.value
    return 0


def _parse_creator(s):
    """'pid 168, WindowServer' -> (168, 'WindowServer')."""
    if not s or not s.startswith("pid "):
        return None, None
    rest = s[4:]
    num, _, name = rest.partition(",")
    try:
        return int(num.strip()), name.strip()
    except ValueError:
        return None, None


def _gpu_client_totals():
    """Return {pid: (name, accumulated_gpu_ns)} summed over all command queues.

    User clients are !registered/!matched, so IOServiceGetMatchingServices does
    not find them. We walk the IOService plane recursively and filter by class.
    """
    totals = {}
    it = c_uint32(0)
    if IOKIT.IORegistryCreateIterator(0, b"IOService",
                                      kIORegistryIterateRecursively, byref(it)) != 0:
        return totals

    while True:
        obj = IOKIT.IOIteratorNext(it)
        if not obj:
            break
        try:
            if not any(IOKIT.IOObjectConformsTo(obj, cls) for cls in GPU_CLIENT_CLASSES):
                continue
            props = c_void_p()
            if IOKIT.IORegistryEntryCreateCFProperties(obj, byref(props), None, 0) != 0 or not props.value:
                continue
            pid, name = _parse_creator(
                from_cfstr(CF.CFDictionaryGetValue(props, _K_CREATOR)))
            usage = CF.CFDictionaryGetValue(props, _K_APPUSAGE)
            if pid is not None and usage:
                ns = 0
                for i in range(CF.CFArrayGetCount(usage)):
                    q = CF.CFArrayGetValueAtIndex(usage, i)
                    ns += _cfnum_i64(CF.CFDictionaryGetValue(q, _K_GPUTIME))
                prev = totals.get(pid, (name, 0))
                totals[pid] = (name or prev[0], prev[1] + ns)
            CF.CFRelease(props)
        finally:
            IOKIT.IOObjectRelease(obj)
    IOKIT.IOObjectRelease(it)
    return totals


def _attach_proc_stats(rows):
    """Fill in each row's cpu_pct and rss_bytes, in one `ps` call for all pids.

    The GPU driver publishes only the API name and the accumulated GPU time per
    client -- no memory figure -- so CPU and memory come from the process itself.
    On Apple Silicon memory is unified, so a process's RSS *is* the memory it is
    costing the SoC; there is no separate VRAM number to report.

    This shells out rather than calling proc_pid_rusage() via ctypes, which would
    be ~2700x cheaper per pid, because rusage is readable only for processes the
    user owns -- and WindowServer, usually the biggest GPU consumer here, is not
    one of them. `ps` reports every process, and its ~27ms is a fixed spawn cost
    (the pid count barely matters), which at a 1s interval is a few percent.

    Best-effort: on any failure the fields are simply left as None.
    """
    if not rows:
        return
    pids = ",".join(str(r["pid"]) for r in rows)
    try:
        out = subprocess.run(["ps", "-o", "pid=,%cpu=,rss=", "-p", pids],
                             capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return
    stats = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            stats[int(parts[0])] = (float(parts[1]), int(parts[2]) * 1024)
        except ValueError:
            continue
    for r in rows:
        cpu, rss = stats.get(r["pid"], (None, None))
        r["cpu_pct"] = cpu
        r["rss_bytes"] = rss


class ProcGPUSampler:
    """Per-process GPU utilization from the driver's per-client GPU time.

    Works without sudo; reads AGXDeviceUserClient accounting from IORegistry.
    """

    def __init__(self):
        self.prev = {}       # pid -> (name, accumulated_ns)
        self.prev_time = None
        self.started = False  # have we taken a baseline snapshot yet?

    def read(self, interval=1.0):
        self.prev = _gpu_client_totals()
        self.prev_time = time.monotonic()
        self.started = True
        time.sleep(interval)
        return self.step()

    def step(self, interval=None):
        """Diff against the previous snapshot without sleeping (for live loop).

        Elapsed time comes from the monotonic clock between snapshots. The first
        call only establishes a baseline, so it always returns no rows.
        ``interval`` is accepted and ignored; it is kept for callers that still
        pass it.
        """
        snap = _gpu_client_totals()
        now = time.monotonic()
        elapsed = (now - self.prev_time) if self.prev_time is not None else 0.0
        rows = []
        if elapsed > 0:
            for pid, (name, ns2) in snap.items():
                if pid in self.prev:
                    base = self.prev[pid][1]
                elif self.started:
                    # A pid we have never seen, but we HAVE snapshotted before:
                    # it is a process that just started, so its baseline is 0 and
                    # the GPU time it has accrued since launch belongs to this
                    # interval. Skipping it instead (as we used to) hid every new
                    # GPU process for a full interval.
                    base = 0
                else:
                    # First snapshot ever: we know nothing about this pid's past,
                    # so a 0 baseline would credit its whole lifetime to one
                    # interval -- WindowServer would report ~17,000,000 ms/s.
                    continue
                dns = ns2 - base
                if dns > 0:
                    rows.append({"pid": pid, "name": name,
                                 "gpu_ms_s": dns / 1e6 / elapsed})
        self.prev = snap
        self.prev_time = now
        self.started = True
        rows.sort(key=lambda r: r["gpu_ms_s"], reverse=True)
        _attach_proc_stats(rows)
        return rows


def _fmt_bytes(n):
    """'1.4G' / '331M' -- a memory figure that fits the narrow MEM column.

    Rounds before choosing the unit, so 1023.7 MiB reads as '1.0G' rather than
    the '1024M' a naive threshold produces.
    """
    if not n:
        return "-"
    for unit, scale in (("T", 1 << 40), ("G", 1 << 30)):
        if round(n / scale, 1) >= 1.0:
            return f"{n / scale:.1f}{unit}"
    return f"{n / (1 << 20):.0f}M"


def render_procs(rows, limit=10):
    """Render a per-process GPU table (like nvidia-smi's process list).

    MEM is the process's RSS: Apple Silicon memory is unified, so that is the
    memory it costs the SoC -- the GPU driver publishes no separate VRAM figure.
    """
    lines = [f"{HEADER} GPU processes{RESET}"]
    if not rows:
        lines.append("  \x1b[2m(no GPU activity)\x1b[0m")
        return lines
    lines.append(f"  {'PID':>7}  {'GPU ms/s':>9}  {'GPU%':>5}  {'CPU%':>6}"
                 f"  {'MEM':>6}  NAME")
    for r in rows[:limit]:
        pct = min(100.0, r["gpu_ms_s"] / 10.0)   # 1000 ms/s == 100%
        cpu = r.get("cpu_pct")
        cpu_s = f"{cpu:.1f}" if cpu is not None else "-"
        lines.append(f"  {r['pid']:>7}  {r['gpu_ms_s']:>9.1f}  {pct:>5.1f}"
                     f"  {cpu_s:>6}  {_fmt_bytes(r.get('rss_bytes')):>6}"
                     f"  {r['name']}")
    return lines


# --- Memory stats (mach host_statistics64 + sysctl, no sudo) -----------------
_libc.mach_host_self.restype = c_uint32
_libc.host_statistics64.argtypes = [c_uint32, c_int, c_void_p, POINTER(c_uint32)]
HOST_VM_INFO64 = 4


class _VMStat(ctypes.Structure):
    _fields_ = [
        ("free", c_uint32), ("active", c_uint32), ("inactive", c_uint32), ("wire", c_uint32),
        ("zero_fill", c_uint64), ("reactivations", c_uint64), ("pageins", c_uint64),
        ("pageouts", c_uint64), ("faults", c_uint64), ("cow_faults", c_uint64),
        ("lookups", c_uint64), ("hits", c_uint64), ("purges", c_uint64),
        ("purgeable", c_uint32), ("speculative", c_uint32),
        ("decompressions", c_uint64), ("compressions", c_uint64),
        ("swapins", c_uint64), ("swapouts", c_uint64),
        ("compressor", c_uint32), ("throttled", c_uint32),
        ("external", c_uint32), ("internal", c_uint32), ("t_uncompressed", c_uint64),
    ]


class _SwapUsage(ctypes.Structure):
    _fields_ = [("total", c_uint64), ("avail", c_uint64), ("used", c_uint64),
                ("pagesize", c_uint32), ("encrypted", c_int)]


def _sysctl_u64(name):
    val = c_uint64(0)
    sz = ctypes.c_size_t(8)
    if _libc.sysctlbyname(name.encode(), byref(val), byref(sz), None, 0) != 0:
        return 0
    return val.value


def _sysctl_str(name):
    try:
        sz = ctypes.c_size_t(0)
        if _libc.sysctlbyname(name.encode(), None, byref(sz), None, 0) != 0 or sz.value == 0:
            return None
        buf = create_string_buffer(sz.value)
        if _libc.sysctlbyname(name.encode(), buf, byref(sz), None, 0) != 0:
            return None
        return buf.value.decode("utf-8", "replace")
    except Exception:
        return None


def machine_name():
    """e.g. 'Apple M4 Pro' on Apple Silicon; falls back gracefully."""
    return _sysctl_str("machdep.cpu.brand_string") or "Mac"


def mem_stats():
    """Return memory usage in bytes, no sudo. Fields default to 0 on failure."""
    out = {"total": 0, "used": 0, "wired": 0, "compressed": 0, "swap_used": 0, "swap_total": 0}
    try:
        total = _sysctl_u64("hw.memsize")
        pg = _sysctl_u64("hw.pagesize") or 16384
        st = _VMStat()
        cnt = c_uint32(ctypes.sizeof(_VMStat) // 4)
        if _libc.host_statistics64(_libc.mach_host_self(), HOST_VM_INFO64, byref(st), byref(cnt)) == 0:
            avail = (st.free + st.inactive + st.speculative + st.purgeable) * pg
            out["total"] = total
            out["used"] = max(0, total - avail)
            out["wired"] = st.wire * pg
            out["compressed"] = st.compressor * pg
        sw = _SwapUsage()
        sz = ctypes.c_size_t(ctypes.sizeof(_SwapUsage))
        if _libc.sysctlbyname(b"vm.swapusage", byref(sw), byref(sz), None, 0) == 0:
            out["swap_used"] = sw.used
            out["swap_total"] = sw.total
    except Exception:
        pass
    return out


# --- Thermal / throttle state (NSProcessInfo.thermalState, no sudo) ----------
# 0 nominal, 1 fair, 2 serious, 3 critical. Rises when the SoC is being
# thermally throttled. Returns -1 if unavailable.
THERMAL_NAMES = {0: "nominal", 1: "fair", 2: "serious", 3: "critical"}


def thermal_state():
    try:
        import ctypes.util as _u
        ctypes.CDLL(_u.find_library("Foundation"))
        objc = ctypes.CDLL(_u.find_library("objc"))
        objc.objc_getClass.restype = c_void_p
        objc.sel_registerName.restype = c_void_p
        objc.objc_msgSend.restype = c_void_p
        objc.objc_msgSend.argtypes = [c_void_p, c_void_p]
        cls = objc.objc_getClass(b"NSProcessInfo")
        pi = objc.objc_msgSend(cls, objc.sel_registerName(b"processInfo"))
        objc.objc_msgSend.restype = c_long
        return int(objc.objc_msgSend(pi, objc.sel_registerName(b"thermalState")))
    except Exception:
        return -1


_CORE_RE = re.compile(r"^([A-Z]+)CPU(\d+)$")   # ECPU000 -> ('E','000'); MCPU14 -> ('M','14')

# A slower tier is an EFFICIENCY cluster only if it is much slower than the
# fastest one. An M4 Pro's E-cluster tops out at 2592 MHz against the P's 4512
# (57%), while an M5 Pro's two tiers are 4380 and 4608 (95%) -- both are
# performance-class, the faster being Apple's "Super" cores. This ratio is what
# tells "E below P" apart from "P below S"; the core-name letters cannot, since
# 'PCPU' means Performance on an M4 and Super on an M5.
_EFFICIENCY_MAX_RATIO = 0.75


def _tier_labels(tiers):
    """Name the CPU tiers from their ladder ceilings: {kind: 'E'|'P'|'S'}.

    ``tiers`` maps a core-name kind ('E', 'P', 'M', ...) to that tier's top
    ladder MHz. Ranking by ceiling rather than by letter is what makes the
    labels agree with powermetrics on both chips:

        M4 Pro   E 2592 / P 4512  -> the slow tier is 57% of the fast: E, P
        M5 Pro   M 4380 / P 4608  -> the slow tier is 95% of the fast: P, S
                                     (so M5's 'PCPU*' correctly reads as S)

    A tier whose ladder is unknown (0) keeps its raw letter rather than being
    guessed at. A single-tier chip is just 'P'.
    """
    known = {k: v for k, v in tiers.items() if v}
    if not known:
        return {k: k for k in tiers}
    top = max(known.values())
    labels = {}
    for kind, ceiling in tiers.items():
        if not ceiling:
            labels[kind] = kind          # unknown ladder: do not invent a tier
        elif ceiling == top:
            # The fastest tier is 'S' only when a *performance*-class tier sits
            # below it; if the tier below is an efficiency one, this is just P.
            others = [v for k, v in known.items() if k != kind]
            has_perf_below = any(v >= top * _EFFICIENCY_MAX_RATIO for v in others)
            labels[kind] = "S" if has_perf_below else "P"
        elif ceiling >= top * _EFFICIENCY_MAX_RATIO:
            labels[kind] = "P"
        else:
            labels[kind] = "E"
    return labels


def _core_kind_and_digits(name):
    """('M', '14') from 'MCPU14'; (None, None) if the name is not a core."""
    m = _CORE_RE.match((name or "").upper())
    if not m:
        return (None, None)
    return (m.group(1), m.group(2))


def group_clusters(cores):
    """Group core channels into clusters, deriving the layout from the names.

    IOReport names cores <KIND>CPU<digits>, but the MEANING of those digits is
    chip-specific and cannot be assumed:

        M4 Pro   ECPU000..ECPU030   PCPU000..PCPU140    <- 3 digits: cluster,core,0
        M5 Pro   PCPU0..PCPU4       MCPU00..MCPU14      <- S is 1 digit (core only),
                                                           M is 2 digits (cluster,core)

    So on an M5 'PCPU*' is the S-cluster and 'MCPU*' the performance cores --
    the letter 'P' means the opposite of what it means on an M4. Hardcoding
    either the letters or the digit positions therefore mislabels the clusters
    on the other chip, which is exactly how a P-cluster came to render as
    "CPU other".

    The layout is instead inferred from the DIGIT COUNT, which is what actually
    distinguishes the two cases. A name carries a cluster index only if it has
    room for one *after* the core number:

        PCPU0 .. PCPU4    1 digit  -> core only  => ONE cluster of 5 cores
        MCPU00 .. MCPU14  2 digits -> cluster,core => TWO clusters of 5
        PCPU000 .. PCPU140  3 digits -> cluster,core,0 => TWO clusters of 5

    Splitting on the leading digit regardless would turn the M5's five S-cores
    (PCPU0..PCPU4, five distinct leading digits) into five one-core clusters.

    The E/P/S letter shown to the user is likewise not taken from the name -- it
    is derived from each tier's ladder ceiling by _tier_labels, so that an M5's
    'PCPU*' correctly reads as S (Super) rather than P.
    """
    tables = DVFS or {}
    by_kind = {}
    order = []
    other = []
    for e in cores:
        kind, digits = _core_kind_and_digits(e.get("name"))
        if kind is None:
            other.append(e)
            continue
        if kind not in by_kind:
            by_kind[kind] = []
            order.append(kind)
        by_kind[kind].append((digits, e))

    # Each kind's ceiling = the top of the ladder its cores' P-state count binds.
    ceilings = {}
    for kind, members in by_kind.items():
        ladder = match_cpu_ladder(_nsteps([e for _, e in members]), tables)
        ceilings[kind] = ladder[-1] if ladder else 0.0
    letters = _tier_labels(ceilings)

    clusters = {}
    corder = []

    def add(key, label, entry):
        if key not in clusters:
            clusters[key] = {"key": key, "label": label, "cores": []}
            corder.append(key)
        clusters[key]["cores"].append(entry)

    for kind in order:
        members = by_kind[kind]
        letter = letters.get(kind, kind)
        # A leading cluster digit exists only when the name has >= 2 digits (one
        # for the cluster, at least one for the core). Single-digit names are
        # core indices within one cluster.
        widths = {len(d) for d, _ in members}
        indexed = len(widths) == 1 and widths.pop() >= 2
        leading = {d[0] for d, _ in members}
        if indexed and len(leading) > 1:
            for digits, e in members:
                idx = digits[0]
                add(f"{letter}{idx}", f"CPU {letter}{idx}-cluster", e)
        else:
            for _, e in members:
                add(letter, f"CPU {letter}-cluster", e)

    for e in other:
        add("?", "CPU other", e)

    return [clusters[k] for k in corder]


def organize(raw):
    """Turn the Sampler's raw channel lists into a render-ready structure.

    Keeps render free of any subgroup names or core-prefix knowledge.
    Returns: {"gpu_pct", "gpu_channels", "clusters":[{"key","label","avg","cores"}]}
    """
    tables = DVFS or {}

    # --- GPU: average across channels if there is more than one ---
    gpu_ch = raw.get("gpu", [])
    gpu_pct = (sum(e["active"] for e in gpu_ch) / len(gpu_ch) * 100) if gpu_ch else 0.0
    gpu_mhz = cluster_freq_mhz(gpu_ch, match_gpu_ladder(tables))

    # --- CPU: group only the per-core (state residency) channels by cluster ---
    cores = [e for e in raw.get("cpu", []) if "COMPLEX" not in (e["subgroup"] or "").upper()]

    out_clusters = []
    for c in group_clusters(cores):
        n = len(c["cores"])
        # Bind this cluster's ladder by its own P-state count, not by its name:
        # the voltage-states numbering differs per chip (see match_cpu_ladder).
        table = match_cpu_ladder(_nsteps(c["cores"]), tables)
        avg = sum(x["active"] for x in c["cores"]) / n * 100
        mhz = cluster_freq_mhz(c["cores"], table)
        # Per-core figures for the 'c' (per-core) view. Computed here rather than
        # in render() so the DVFS tables stay out of the display layer.
        per_core = []
        for x in c["cores"]:
            per_core.append({"name": x["name"], "pct": x["active"] * 100,
                             "mhz": cluster_freq_mhz([x], table)})
        out_clusters.append({**c, "avg": avg, "count": n, "mhz": mhz,
                             "per_core": per_core})

    return {"gpu_pct": gpu_pct, "gpu_channels": gpu_ch, "gpu_mhz": gpu_mhz,
            "clusters": out_clusters,
            "power": raw.get("power", {}),
            "power_avg": raw.get("power_avg", {}), "power_peak": raw.get("power_peak", {})}


# --- Terminal display -------------------------------------------------------
ESC = "\x1b["
HIDE_CURSOR = ESC + "?25l"
SHOW_CURSOR = ESC + "?25h"
CLEAR = ESC + "2J" + ESC + "H"
HOME = ESC + "H"
CLEAR_TO_END = ESC + "0J"   # clear from cursor to end of screen (removes leftovers)

HEADER = "\x1b[1;92m"       # bold green for section titles
RESET = "\x1b[0m"

# Rows in each history graph. Keep this EVEN: a row's value is (level+1)/height,
# so a true 50% row -- the one vgraph() labels as the half-scale mark -- exists
# only at even heights. At the previous height of 5 the rows sat at
# 20/40/60/80/100 and there was no 50% row to label at all.
GRAPH_HEIGHT = 6

# 1/8-step blocks that fill from bottom to top
EIGHTHS = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


def color_for(pct):
    # Bold ("1;") as well as bright ("9x"): on most terminal themes the bright
    # colours alone render fairly washed out, and these bars are the thing the
    # eye should land on first.
    if pct >= 80:
        return "\x1b[1;91m"  # red
    if pct >= 50:
        return "\x1b[1;93m"  # yellow
    return "\x1b[1;92m"      # green


def vgraph(history, height=8, width=48, label_max=None, label_unit="%",
           color=None):
    """Draw history(%) as a vertical bar graph that grows from bottom to top.

    Oldest value on the left, newest on the right; column height is utilization.
    The top and middle rows carry a y-axis label. If label_max is given, those
    labels show that scale (e.g. watts) instead of %, with the top == label_max.
    """
    vals = list(history)[-width:]
    vals = [0.0] * (width - len(vals)) + vals  # pad the left with empty values

    # Convert each column to a count of 1/8 ticks
    ticks = [int(round(max(0.0, min(100.0, v)) / 100 * height * 8)) for v in vals]

    rows = []
    for r in range(height):                     # r=0 is the top row
        level = height - 1 - r                   # bottom-referenced cell for this row
        cells = []
        for i, t in enumerate(ticks):
            fill = t - level * 8
            fill = 0 if fill < 0 else (8 if fill > 8 else fill)
            ch = EIGHTHS[fill]
            c = (color or color_for(vals[i])) if fill > 0 else ""
            cells.append(f"{c}{ch}\x1b[0m" if fill > 0 else ch)
        # Label the full-scale row and the half-scale row. Each row spans a band,
        # and the row's top edge is (level+1)/height -- so a true 50% row exists
        # only when height is even (at the old height=5 the rows sat at
        # 20/40/60/80/100 and there was simply no 50% row to label).
        axis = (level + 1) / height * 100
        if r == 0 or abs(axis - 50) < 1e-9:
            if label_max is not None:
                lab = f"{axis / 100 * label_max:.0f}{label_unit}"
            else:
                lab = f"{axis:.0f}{label_unit}"
        else:
            lab = ""
        rows.append(f"  {lab:>4}│{''.join(cells)}")
    rows.append("      └" + "─" * width)
    return rows


def term_size():
    import shutil
    s = shutil.get_terminal_size(fallback=(80, 24))
    return s.columns, s.lines


def bar_width_for(cols):
    """Inner bar width that fits the terminal (leaves room for the box border)."""
    return max(10, min(140, cols - 10))


TRACK = "\x1b[90m"          # dim gray for the unfilled gauge track


def gauge_bar(frac, width):
    """Solid '█' fill over a dim '░' track.

    The fill used to be '▏' (a left-eighth block), which paints only 1/8 of each
    cell -- so the bar read as washed out no matter which colour it was given.
    """
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    c = color_for(frac * 100)
    return f"{c}{'█' * filled}{TRACK}{'░' * (width - filled)}{RESET}"


def bracket_gauge(frac, width):
    """The bracketed gauge, '[████░░░░]'. The one place the chrome is defined.

    Both the dashboard (via hgauge) and the per-core view render through this, so
    the two cannot drift apart -- which is exactly how the per-core bars ended up
    using a different glyph than the cluster bars.
    """
    return f"[{gauge_bar(frac, width)}]"


def hgauge(label, frac, width, value=""):
    """asitop-style gauge: a title line, then a filled gauge bar.

    Returns a list of two strings.
    """
    title = f"  {label}" + (f"   {value}" if value else "")
    return [title, f"  {bracket_gauge(frac, width)}"]


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _visible_len(s):
    return len(_ANSI_RE.sub("", s))


def _truncate_visible(s, width):
    """Cut s to `width` visible columns, keeping ANSI escapes intact.

    Escape sequences cost no width, so they are copied through; a trailing RESET
    is appended if anything was dropped, otherwise the color would bleed on.
    """
    if _visible_len(s) <= width:
        return s
    out, shown, pos = [], 0, 0
    for m in _ANSI_RE.finditer(s):
        for ch in s[pos:m.start()]:
            if shown >= width:
                return "".join(out) + RESET
            out.append(ch)
            shown += 1
        out.append(m.group())
        pos = m.end()
    for ch in s[pos:]:
        if shown >= width:
            break
        out.append(ch)
        shown += 1
    return "".join(out) + RESET


def wrap_box(lines, cols, title=""):
    """Wrap content lines in a full box border, title embedded in the top edge.

    Content wider than the box is truncated so the right border never shifts.
    """
    inner = max(4, cols - 2)
    t = f" {title} " if title else ""
    tv = _visible_len(t)
    if tv > inner:
        t = _truncate_visible(t, inner)
        tv = _visible_len(t)
    out = ["┌" + t + "─" * (inner - tv) + "┐"]
    for ln in lines:
        ln = _truncate_visible(ln, inner)
        pad = inner - _visible_len(ln)
        out.append("│" + ln + " " * max(0, pad) + "│")
    out.append("└" + "─" * inner + "┘")
    return out


def _freq_txt(mhz):
    """'@ 1398 MHz', or nothing when the clock is unknown."""
    if not mhz:
        return ""
    return f"@ {mhz:.0f} MHz"


def render_cores(view, width, limit=None):
    """Render every CPU core individually, grouped by cluster.

    The dashboard shows one gauge per cluster (the E/P averages); this is the
    same data broken out per core, so a single pegged core is visible instead of
    being averaged away.
    """
    # Same gauge as the dashboard's cluster bars (gauge_bar, in brackets), so the
    # two views read as one UI. Leave room for the name, the "100.00%" column and
    # the "@ 100% DVFS" suffix, or the bar pushes them past the border and
    # wrap_box() truncates them away.
    bw = max(8, width - 32)
    lines = []
    for c in view.get("clusters", []):
        if limit is not None and len(lines) >= limit:
            break
        ftxt = _freq_txt(c.get("mhz", 0.0))
        head = f"{HEADER} {c['label']}  ({c['count']} cores)  avg {c['avg']:.1f}%"
        lines.append(head + (f"  {ftxt}" if ftxt else "") + RESET)
        for core in c.get("per_core", []):
            if limit is not None and len(lines) >= limit:
                break
            cf = _freq_txt(core.get("mhz", 0.0))
            pct = core["pct"]
            lines.append("  %-8s %s %6.2f%%  %s"
                         % (core["name"], bracket_gauge(pct / 100, bw), pct, cf))
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        lines.append("  \x1b[2m(no CPU cores found)\x1b[0m")
    return lines


def render(view, cols=80, gpu_hist=None, procs=None, height=None, soc_hist=None,
           process_only=False, single_sample=False, core_only=False):
    """Draw the organize() result (view). Does no data-structure reasoning.

    ``single_sample`` suppresses the avg/peak columns, which carry no information
    when only one sample has been taken (they would all just equal the current).
    """
    width = bar_width_for(cols)
    lines = []

    # Box title: app name + machine name + thermal/throttle state.
    title = f"Soltop v{__version__} · {machine_name()}"
    ts = thermal_state()
    if ts >= 0:
        tcolor = {0: "\x1b[92m", 1: "\x1b[93m", 2: "\x1b[91m", 3: "\x1b[91m"}.get(ts, "")
        tname = THERMAL_NAMES.get(ts, "?")
        thr = " throttling" if ts >= 1 else ""
        title += f"   thermal: {tcolor}{tname}{thr}{RESET}"

    def _fit(lines, title):
        """Pad/clip content to the frame and draw the border."""
        if height:
            content_height = max(0, height - 2)
            del lines[content_height:]
            while len(lines) < content_height:
                lines.append("")
        return ("\x1b[K\n").join(wrap_box(lines, cols, title)) + "\x1b[K"

    if process_only:
        title = f"Soltop v{__version__} · GPU Processes · p: dashboard · q: quit"
        limit = max(1, height - 4) if height else 10
        lines.extend(render_procs(procs or [], limit=limit))
        return _fit(lines, title)

    if core_only:
        title = f"Soltop v{__version__} · CPU Cores · c: dashboard · q: quit"
        limit = max(1, height - 2) if height else None
        lines.extend(render_cores(view, width, limit=limit))
        return _fit(lines, title)

    title += "   p: processes   c: cores   q: quit"

    freq_txt = _freq_txt(view.get("gpu_mhz", 0.0))
    cur = view["gpu_pct"]
    if single_sample:
        stats = ""
    elif gpu_hist:
        stats = f"  (avg {sum(gpu_hist) / len(gpu_hist):.1f}%  peak {max(gpu_hist):.1f}%)"
    else:
        stats = ""
    lines.append(f"{HEADER} GPU Usage: {cur:.1f}%{stats}  {freq_txt}{RESET}")
    if gpu_hist is not None:
        lines.extend(vgraph(gpu_hist, height=GRAPH_HEIGHT, width=max(10, width - 7)))
    lines.append("")

    # Power: cur/avg/peak table for components; total as an asitop-style gauge.
    power = view.get("power", {})
    pavg = view.get("power_avg", {})
    ppeak = view.get("power_peak", {})
    if power:
        # Components on one line, each with W unit and cur/avg/peak values.
        def triple(lbl):
            if single_sample:
                return f"{lbl} {power[lbl] / 1000:.1f}W"
            return (f"{lbl} {power[lbl] / 1000:.1f}/"
                    f"{pavg.get(lbl, 0) / 1000:.1f}/{ppeak.get(lbl, 0) / 1000:.1f}W")
        comp = " | ".join(triple(lbl) for lbl in POWER_LABELS if lbl in power)
        soc_w = power.get("SoC", 0) / 1000
        if single_sample:
            pstats = ""
        else:
            pstats = (f"  (avg {pavg.get('SoC', 0) / 1000:.1f}W"
                      f"  peak {ppeak.get('SoC', 0) / 1000:.1f}W)")
        lines.append(f"{HEADER} Total Power: {soc_w:.1f}W{pstats}{RESET}")
        # Power history graph: fixed 110 W full-scale, always green (we don't
        # know the real per-machine limit, so don't imply thresholds by color).
        if soc_hist is not None:
            scale = 110.0
            norm = [min(100.0, (w / scale) * 100) for w in soc_hist]
            lines.extend(vgraph(norm, height=GRAPH_HEIGHT, width=max(10, width - 7),
                                label_max=scale, label_unit="W",
                                color="\x1b[1;92m"))
        lines.append(f"  {comp}" + ("" if single_sample else "   (cur/avg/peak)"))
        lines.append("")

    # Memory: asitop-style gauge + text breakdown (wired / compressed / swap).
    mem = mem_stats()
    if mem.get("total"):
        gb = 1_000_000_000
        total = mem["total"] / gb
        used = mem["used"] / gb
        frac = mem["used"] / mem["total"] if mem["total"] else 0.0
        lines.append(f"{HEADER} Memory{RESET}")
        val = (f"{used:.1f}/{total:.1f} GB ({frac * 100:.0f}%)   "
               f"wired {mem['wired'] / gb:.1f}G | "
               f"compressed {mem['compressed'] / gb:.1f}G | "
               f"swap {mem['swap_used'] / gb:.1f}/{mem['swap_total'] / gb:.1f}G")
        lines.extend(hgauge("RAM", frac, width, val))
        lines.append("")

    # CPU: core counts in the title; one asitop-style gauge per cluster + freq.
    counts = "  ".join(f"{c['key']}:{c['count']}" for c in view["clusters"])
    lines.append(f"{HEADER} CPU  ({counts}){RESET}")
    for c in view["clusters"]:
        ftxt = _freq_txt(c.get("mhz", 0.0))
        val = f"{c['avg']:.1f}%" + (f"  {ftxt}" if ftxt else "")
        lines.extend(hgauge(c["label"], c["avg"] / 100, width, val))
    lines.append("")

    # Per-process GPU table LAST: its row count changes, so keep it at the
    # bottom to avoid shifting everything above it. Limit rows to fit the screen
    # (reserve the top/bottom border + column-title rows).
    if height:
        limit = max(1, height - len(lines) - 6)
    else:
        limit = 10
    lines.extend(render_procs(procs or [], limit=limit))

    if lines and lines[-1] == "":
        lines.pop()

    # _fit() pads/clips to the screen height (on very short terminals the
    # lower-priority content at the bottom is omitted), draws the outer border,
    # and clears each line end so longer leftovers from the prior frame go away.
    return _fit(lines, title)


class KeyReader:
    """Read single terminal keys without blocking, restoring mode on exit."""

    def __init__(self, stream):
        self.stream = stream
        self.fd = None
        self.attrs = None

    def __enter__(self):
        if self.stream.isatty():
            import termios
            import tty
            self.fd = self.stream.fileno()
            self.attrs = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def read_available(self):
        if self.fd is None:
            return ""
        import os
        import select
        keys = []
        while select.select([self.fd], [], [], 0)[0]:
            data = os.read(self.fd, 1)
            if not data:
                break
            keys.append(data.decode("utf-8", "ignore"))
        return "".join(keys)

    def __exit__(self, exc_type, exc_value, traceback):
        if self.fd is not None and self.attrs is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.attrs)


# Consecutive Sampler rebuilds live() will attempt before giving up. A transient
# IOReport failure recovers on the first retry; anything that fails this many
# times running is not transient, and spinning on it forever helps nobody.
LIVE_MAX_RETRIES = 3


def live(interval=1.0, cols_override=None):
    import sys

    sampler = Sampler()
    proc_sampler = ProcGPUSampler()
    gpu_hist = deque(maxlen=200)
    soc_hist = deque(maxlen=200)
    # Clear the whole screen only once; afterwards just move the cursor home
    # and overwrite in place to avoid flicker.
    print(HIDE_CURSOR + CLEAR, end="", flush=True)
    last_size = None
    process_only = False
    core_only = False
    retries = 0
    try:
        with KeyReader(sys.stdin) as keys:
            while True:
                try:
                    view = organize(sampler.read(interval))
                    retries = 0
                except RuntimeError:
                    # read() closes the Sampler and raises when IOReport keeps
                    # failing, so without this a transient hiccup would kill the
                    # monitor with a traceback. Rebuild and carry on -- but only
                    # a bounded number of times in a row: retrying forever would
                    # spin, rebuilding a subscription (an IOReportCopyAllChannels
                    # scan over ~11k channels) several times a second behind a
                    # frozen screen, with the user told nothing.
                    retries += 1
                    if retries > LIVE_MAX_RETRIES:
                        raise
                    sampler.close()
                    sampler = Sampler()
                    time.sleep(interval)     # don't hot-spin while it's broken
                    continue
                gpu_hist.append(view["gpu_pct"])
                soc_hist.append(view.get("power", {}).get("SoC", 0) / 1000)
                try:
                    procs = proc_sampler.step(interval)
                except Exception:
                    procs = []
                # Handle each key in order: 'p' toggles the process view, 'c' the
                # per-core view (each returns to the dashboard when pressed again,
                # and the two views are mutually exclusive), 'q'/ESC quits.
                for k in keys.read_available().lower():
                    if k == "p":
                        process_only = not process_only
                        core_only = False
                    elif k == "c":
                        core_only = not core_only
                        process_only = False
                    elif k in ("q", "\x1b", "\x03"):
                        return
                tcols, rows = term_size()
                cols = cols_override or tcols
                # On resize, wipe the screen once so wrapped/old lines don't linger.
                if (cols, rows) != last_size:
                    print(CLEAR, end="")
                    last_size = (cols, rows)
                frame = render(view, cols, gpu_hist, procs, height=rows,
                               soc_hist=soc_hist, process_only=process_only,
                               core_only=core_only)
                # Overwrite the whole frame in one write, then clear any lines below.
                print(HOME + frame + CLEAR_TO_END, end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sampler.close()
        print(SHOW_CURSOR + "\n", end="", flush=True)


def main():
    import argparse

    p = argparse.ArgumentParser(prog="soltop", description="Apple Silicon GPU/CPU usage monitor")
    def positive_float(value):
        import math
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise argparse.ArgumentTypeError("must be a finite number greater than 0")
        return parsed

    def nonnegative_int(value):
        parsed = int(value)
        if parsed < 0:
            raise argparse.ArgumentTypeError("must be 0 or greater")
        return parsed

    p.add_argument("-i", "--interval", type=positive_float, default=1.0,
                   help="sampling interval (seconds)")
    p.add_argument("-c", "--cols", type=nonnegative_int, default=0,
                   help="terminal columns to use (0 = auto-fit)")
    p.add_argument("--once", action="store_true", help="print once and exit")
    p.add_argument("--version", action="version", version=f"soltop {__version__}")
    args = p.parse_args()
    cols_override = args.cols or None

    # IOReport can refuse to subscribe (unsupported machine, or it simply keeps
    # failing). Report that as a message, not a Python traceback.
    try:
        if args.once:
            sampler = Sampler()
            try:
                try:
                    proc_sampler = ProcGPUSampler()
                    proc_sampler.step()  # establish the process baseline before the wait
                except Exception:
                    proc_sampler = None
                view = organize(sampler.read(args.interval))
                try:
                    procs = proc_sampler.step() if proc_sampler else []
                except Exception:
                    procs = []
                tcols, rows = term_size()
                # One sample: no history worth graphing, and no meaningful avg/peak.
                print(render(view, cols_override or tcols, gpu_hist=None, procs=procs,
                             height=rows, soc_hist=None, single_sample=True))
            finally:
                sampler.close()
        else:
            live(args.interval, cols_override)
    except RuntimeError as e:
        import sys
        print(f"soltop: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
