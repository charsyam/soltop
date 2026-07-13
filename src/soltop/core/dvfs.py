"""Frequency ladders: decode the voltage-states tables, bind one to a cluster.

The naming and numbering in Apple's IORegistry is NOT stable across chips, so
nothing here keys off a name: a ladder is identified by its shape and encoding.
Where that is ambiguous, NO clock is reported rather than a wrong one -- see
tools/fixtures/ for the M4 Pro / M5 Pro captures this is calibrated against.

This module is pure policy over raw tables; it holds no IOReport state.
"""
import json
import os
import re
import tempfile

from ..ffi import (CF, IOKIT, c_void_p, c_uint32, c_long, byref,
                   from_cfstr, kIORegistryIterateRecursively)


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


# Plausibility bounds. A derived ladder outside these is not a CPU/GPU DVFS
# table (or the encoding changed), and printing a clock from it would be a
# fabrication -- so the caller shows no MHz instead.
_SANE_MHZ = (100.0, 10_000.0)

_CACHE_SCHEMA = 1


def _dvfs_cache_identity():
    """Return the hardware/OS identity that makes decoded tables reusable."""
    try:
        from .system import _sysctl_str
        model = _sysctl_str("hw.model")
        os_build = _sysctl_str("kern.osversion")
        return (model, os_build) if model and os_build else None
    except Exception:
        return None


def _dvfs_cache_path():
    root = os.environ.get(
        "SOLTOP_CACHE_DIR",
        os.path.expanduser("~/Library/Caches/soltop"),
    )
    return os.path.join(root, "dvfs.json")


def _valid_cached_tables(value):
    """Validate untrusted JSON and restore the tuple/list table shape."""
    if not isinstance(value, dict) or not value:
        return None
    lo, hi = _SANE_MHZ
    out = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not _VOLTAGE_STATE_RE.match(key):
            return None
        if not isinstance(entry, list) or len(entry) != 2:
            return None
        kind, ladder = entry
        if kind not in ("gpu", "sram", "period") or not isinstance(ladder, list):
            return None
        if not ladder or any(isinstance(v, bool) or not isinstance(v, (int, float))
                             for v in ladder):
            return None
        values = [float(v) for v in ladder]
        if values != sorted(values) or values[0] < lo or values[-1] > hi:
            return None
        out[key] = (kind, values)
    return out


def _load_dvfs_cache():
    identity = _dvfs_cache_identity()
    if identity is None:
        return None
    try:
        with open(_dvfs_cache_path(), encoding="utf-8") as f:
            cached = json.load(f)
        # Reject an unknown format before looking at any version-specific fields.
        if cached.get("schema") != _CACHE_SCHEMA:
            return None
        if (cached.get("model"), cached.get("os_build")) != identity:
            return None
        return _valid_cached_tables(cached.get("tables"))
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        return None


def _save_dvfs_cache(tables):
    if not tables:
        return
    identity = _dvfs_cache_identity()
    if identity is None:
        return
    path = _dvfs_cache_path()
    tmp = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".dvfs-", suffix=".tmp",
                                   dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"schema": _CACHE_SCHEMA, "model": identity[0],
                       "os_build": identity[1], "tables": tables},
                      f, separators=(",", ":"))
        os.replace(tmp, path)
        tmp = None
    except (OSError, TypeError, ValueError):
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass

# An Apple GPU clocks far below its CPU (an M4 Pro tops out at 1578 MHz, an M5
# Pro at 1620). Several unrelated Hz tables sit alongside it in the IORegistry
# -- 801..2004 and 732..2472 on an M5 -- so a ceiling is what tells them apart.
#
# This is the one number here that a future chip can outgrow: it must sit above
# every real GPU clock and below the nearest impostor, and those bounds are
# tighter than they look. Raising it to 2400 to "leave headroom" immediately
# broke the M4 Pro, whose voltage-states8 (744..2364) then outranked the real
# GPU table and reported the GPU at 2364 MHz. The impostors sit at 2004/2364 on
# an M4 and 2004/2472 on an M5, so the usable window is roughly [1620, 2004).
#
# When a future GPU does clock past this, its ladder is dropped and the GPU
# shows NO frequency -- wrong, but silently-wrong is the failure we are buying
# out of: it will not print a CPU ladder as a GPU clock (see match_gpu_ladder).


# An Apple GPU clocks far below its CPU (an M4 Pro tops out at 1578 MHz, an M5
# Pro at 1620). Several unrelated Hz tables sit alongside it in the IORegistry
# -- 801..2004 and 732..2472 on an M5 -- so a ceiling is what tells them apart.
#
# This is the one number here that a future chip can outgrow: it must sit above
# every real GPU clock and below the nearest impostor, and those bounds are
# tighter than they look. Raising it to 2400 to "leave headroom" immediately
# broke the M4 Pro, whose voltage-states8 (744..2364) then outranked the real
# GPU table and reported the GPU at 2364 MHz. The impostors sit at 2004/2364 on
# an M4 and 2004/2472 on an M5, so the usable window is roughly [1620, 2004).
#
# When a future GPU does clock past this, its ladder is dropped and the GPU
# shows NO frequency -- wrong, but silently-wrong is the failure we are buying
# out of: it will not print a CPU ladder as a GPU clock (see match_gpu_ladder).
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
    cached = _load_dvfs_cache()
    if cached is not None:
        return cached
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
    _save_dvfs_cache(tables)
    return tables


def _rank_key(item):
    """Order tables by key number, so the choice is stable, not dict-ordered."""
    key, _ = item
    m = _VOLTAGE_STATE_RE.match(key)
    return int(m.group(1)) if m else 1 << 30


def _is_sram(key):
    """True for a '-sram' table.

    Every '-sram' key duplicates the ladder of its non-sram twin (in kHz for the
    CPU tables, in Hz for the GPU's), so it carries no information the twin does
    not -- but it DOES add a spurious candidate to every match, which is how a
    CPU ladder came to be offered to the GPU. Exclude them from matching.

    This reads the key name, which the rest of this module is at pains not to do.
    It is safe here because '-sram' is a structural suffix, not a chip-specific
    number: a table either has a twin or it does not, on every chip seen so far.
    """
    return key.endswith("-sram")


def match_cpu_ladder(nsteps, tables):
    """Pick the CPU ladder for a cluster with ``nsteps`` P-states, or [].

    IOReport names each core's P-states V0P18, V1P17, ... so the number of
    non-idle states IS the length of that cluster's ladder, and a CPU table with
    exactly that many entries is the cluster's.

    Verified on an M5 Pro, where the key numbering is nothing like the M4's:
    the S-cluster (20 steps) binds to voltage-states5, P0/P1 (15 steps each) to
    voltage-states22/23 -- and the E-cluster key the old code hardcoded does not
    exist on that chip.

    A step count can match SEVERAL tables, and that is normal: a chip's two
    performance clusters expose the same ladder (an M4 Pro has voltage-states5
    and 13, both 1260..4512; an M5 Pro has 22 and 23, both 1344..4380), and
    powermetrics likewise prints one ladder for both. So an ambiguity whose
    candidates AGREE is not an ambiguity -- take the ladder.

    But if the candidates DISAGREE, the step count alone cannot say which one is
    this cluster's, and picking the lowest-numbered would be a guess rendered as
    a fact. Report no clock instead, and let the caller show nothing: that is the
    same bargain the rest of the DVFS code makes (see load_dvfs, match_gpu_ladder,
    POWER_SANE_MAX_MW). No chip we have data for hits this, but nothing in the
    IORegistry promises it cannot.
    """
    if not nsteps:
        return []
    candidates = [ladder for key, (kind, ladder) in sorted(tables.items(), key=_rank_key)
                  if kind == "period" and not _is_sram(key) and len(ladder) == nsteps]
    return _one_ladder(candidates)


def _one_ladder(candidates):
    """The ladder the candidates agree on, or [] if they disagree.

    Agreement is the whole test. Several tables matching is routine (two
    performance clusters share a ladder), and identical candidates are no
    ambiguity at all. Candidates that DIFFER mean the selector cannot tell which
    is this cluster's -- so report no clock rather than render a guess as a fact.
    """
    if not candidates:
        return []
    first = candidates[0]
    if any(other != first for other in candidates[1:]):
        return []
    return first


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

    Among the survivors the lowest key number wins -- voltage-states9 on both
    chips we have data for.

    KNOWN WEAKNESS: unlike the CPU, the GPU has no per-cluster step count to
    match against, so this cannot fall back on "if the candidates disagree,
    report nothing" -- the surviving Hz tables genuinely differ (an M4 Pro keeps
    338..1578 in voltage-states9 and 14, and an unrelated 338..1470 in 31), and
    refusing to choose would drop the GPU clock on hardware where it is correct
    today. So this ranks rather than abstains, and cap_mhz is a bound on Apple's
    GPU clocks rather than anything the IORegistry asserts. If a future GPU
    clocks past cap_mhz its ladder is dropped and the GPU shows no frequency --
    wrong, but silent-wrong is what we are avoiding: it will not report a CPU
    ladder as a GPU clock, which is the failure this replaced.
    """
    candidates = [ladder for key, (kind, ladder) in sorted(tables.items(), key=_rank_key)
                  if kind == "gpu" and not _is_sram(key) and ladder[-1] <= cap_mhz]
    return candidates[0] if candidates else []


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


# Populated lazily on the first Sampler. Read it through tables() rather than
# importing the name: `from .dvfs import DVFS` binds a COPY, so a later
# assignment here (or in a test) would be invisible to the importer.
DVFS = None


def tables():
    """The decoded voltage-states tables, or {} before the first Sampler."""
    return DVFS or {}


def set_tables(t):
    """Install the tables (the Sampler does this once; tests override it)."""
    global DVFS
    DVFS = t
