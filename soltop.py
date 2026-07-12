#!/usr/bin/env python3
"""Soltop — read GPU utilization and CPU cluster residency via IOReport.

Computes GPU/CPU active utilization from P-State residency on Apple Silicon.
"""
import ctypes
import ctypes.util
import time
from collections import deque

__version__ = "0.4.1"

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


def _fallback_state_subgroups(all_ch):
    """Rename-tolerant scan, used only when no canonical subgroup is present.

    Keeps the original 'works even if Apple renames things' property, but
    excludes the status-register subgroups that are not utilization.
    """
    wanted, seen = [], set()
    for chan in iter_channels(all_ch):
        group = from_cfstr(IOR.IOReportChannelGetGroup(chan))
        subgroup = from_cfstr(IOR.IOReportChannelGetSubGroup(chan))
        if not (classify_group(group) and subgroup):
            continue
        u = subgroup.upper()
        if "PERFORMANCE STATE" not in u or any(b in u for b in _NOT_UTIL):
            continue
        pair = (group, subgroup)
        if pair not in seen:
            seen.add(pair)
            wanted.append(pair)
    return wanted


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

    available, seen = [], set()
    for chan in iter_channels(all_ch):
        group = from_cfstr(IOR.IOReportChannelGetGroup(chan))
        subgroup = from_cfstr(IOR.IOReportChannelGetSubGroup(chan))
        if classify_group(group) and subgroup:
            seen.add((group, subgroup))

    for kind, names in _UTIL_SUBGROUPS.items():
        for group, subgroup in seen:
            if classify_group(group) == kind and subgroup in names:
                available.append((group, subgroup))

    if not available:
        available = _fallback_state_subgroups(all_ch)
    CF.CFRelease(all_ch)

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
ENERGY_KEYS = {"CPU Energy": "CPU", "GPU": "GPU", "ANE": "ANE", "DRAM": "DRAM"}
# Display order; "SoC" is derived as the sum of the components above.
POWER_ORDER = ("CPU", "GPU", "ANE", "DRAM", "SoC")
# Full-scale (mW) for each power bar; the bar still grows if this is exceeded.
POWER_SCALE = {"CPU": 40000, "GPU": 40000, "ANE": 12000, "DRAM": 15000, "SoC": 80000}


# --- DVFS frequency tables (voltage-states in IORegistry, no sudo) -----------
# Only the GPU table is stored in a unit we can convert exactly (Hz -> MHz).
# The CPU tables use a raw unit whose absolute scale is undocumented and varies
# by generation, so we do NOT invent an MHz number for them -- a wrong-but-precise
# "@ 3891 MHz" is worse than no number. Instead the CPU ladder is reported as a
# 0..100 position ("DVFS %"), which is exactly what the raw table supports.
_VOLTAGE_STATE_KEYS = {"E": "voltage-states1", "P": "voltage-states5", "GPU": "voltage-states9"}
# Tables whose values are true frequencies and can be shown in MHz.
_ABSOLUTE_FREQ_CLUSTERS = ("GPU",)


def _read_u32_data(keynames):
    """Read IORegistry CFData properties as lists of little-endian uint32.

    Takes several key names and returns {keyname: [uint32, ...]}, walking the
    IOService plane only once -- the traversal is by far the expensive part, so
    doing it per key would triple the startup cost for no benefit.
    """
    CF.CFDataGetLength.restype = c_long
    CF.CFDataGetLength.argtypes = [c_void_p]
    CF.CFDataGetBytePtr.restype = c_void_p
    CF.CFDataGetBytePtr.argtypes = [c_void_p]
    it = c_uint32(0)
    if IOKIT.IORegistryCreateIterator(0, b"IOService", kIORegistryIterateRecursively, byref(it)) != 0:
        return {}
    keys = {name: cfstr(name) for name in keynames}
    out = {}
    try:
        while len(out) < len(keys):
            o = IOKIT.IOIteratorNext(it)
            if not o:
                break
            props = c_void_p()
            if IOKIT.IORegistryEntryCreateCFProperties(o, byref(props), None, 0) == 0 and props.value:
                for name, key in keys.items():
                    if name in out:
                        continue
                    v = CF.CFDictionaryGetValue(props, key)
                    if not v:
                        continue
                    n = CF.CFDataGetLength(v)
                    if n >= 4:
                        # from_address copies into a Python list before props is
                        # released, so the CFData bytes need not outlive this call.
                        out[name] = list((c_uint32 * (n // 4)).from_address(
                            CF.CFDataGetBytePtr(v)))
                CF.CFRelease(props)
            IOKIT.IOObjectRelease(o)
    finally:
        IOKIT.IOObjectRelease(it)
        for key in keys.values():
            CF.CFRelease(key)
    return out


def load_dvfs():
    """Return {cluster: {"values": [ascending], "unit": "MHz"|"%"}} best-effort.

    GPU values are true MHz. CPU ladders are reported as a percentage of the
    cluster's own top step, because their raw unit has no known MHz conversion.
    """
    try:
        data = _read_u32_data(list(_VOLTAGE_STATE_KEYS.values()))
    except Exception:
        return {}
    tables = {}
    for cluster, key in _VOLTAGE_STATE_KEYS.items():
        raw = data.get(key)
        if not raw:
            continue
        freqs = [f for f in raw[0::2] if f]     # first uint of each (freq, volt) pair
        if not freqs:
            continue
        if cluster in _ABSOLUTE_FREQ_CLUSTERS and max(freqs) > 1_000_000:
            tables[cluster] = {"values": sorted(f / 1e6 for f in freqs), "unit": "MHz"}
        else:
            hi = max(freqs)
            tables[cluster] = {"values": sorted(f / hi * 100 for f in freqs), "unit": "%"}
    return tables


def _pstate_index(name):
    """Parse the P-state index from a state name like 'V0P18' -> 18."""
    if not name or "P" not in name:
        return None
    tail = name.rsplit("P", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return None


def cluster_freq_mhz(cores, table):
    """Residency-weighted active DVFS level over a cluster's cores.

    ``table`` is either a plain ascending list of values or the {"values","unit"}
    dict produced by load_dvfs(). Returns the weighted value (0.0 if unknown);
    use cluster_freq(), which also reports the unit, for display.
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


def cluster_freq(cores, table):
    """As cluster_freq_mhz(), but also returns the unit -> (value, unit)."""
    unit = table.get("unit", "MHz") if isinstance(table, dict) else "MHz"
    return cluster_freq_mhz(cores, table), unit


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
        self.subscribed, self.chans, self.sub = build_subscription()
        self.prev = IOR.IOReportCreateSamples(self.subscribed, self.chans, None)
        self.prev_time = time.monotonic()
        # Retain channel metadata across samples; missing/parked channels have
        # their measurements reset to zero in read().
        self.last = {}          # key -> most recent entry
        self.order = {}         # group -> [key, ...] preserve observed order
        self.power = {}         # label -> power in mW (from Energy Model)
        self.power_max = {}     # label -> bar full-scale (floored, for scaling)
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

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _recreate(self):
        """Release resources and re-subscribe when the subscription drops."""
        self._release()
        self.subscribed, self.chans, self.sub = build_subscription()
        self.prev = IOR.IOReportCreateSamples(self.subscribed, self.chans, None)
        self.prev_time = time.monotonic()

    def read(self, interval=1.0):
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
                raise RuntimeError("samples failed (still failing after re-subscribe)")

        # The previous sample is no longer needed: release and swap in the new one.
        CF.CFRelease(self.prev)
        self.prev = cur
        elapsed = max(cur_time - self.prev_time, 1e-9)
        self.prev_time = cur_time

        observed = set()
        current_power = {lbl: 0.0 for lbl in ("CPU", "GPU", "ANE", "DRAM")}

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
        self.power["SoC"] = sum(self.power[l] for l in ("CPU", "GPU", "ANE", "DRAM"))
        # Bar scale: a sensible per-component full-scale, grown if exceeded.
        # Also accumulate peak and running average per component.
        self.power_cnt += 1
        for lbl, mw in self.power.items():
            floor = POWER_SCALE.get(lbl, 20000.0)
            self.power_max[lbl] = max(self.power_max.get(lbl, floor), mw)
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
                "power": dict(self.power), "power_max": dict(self.power_max),
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


class ProcGPUSampler:
    """Per-process GPU utilization from the driver's per-client GPU time.

    Works without sudo; reads AGXDeviceUserClient accounting from IORegistry.
    """

    def __init__(self):
        self.prev = {}   # pid -> (name, accumulated_ns)
        self.prev_time = None

    def read(self, interval=1.0):
        self.prev = _gpu_client_totals()
        self.prev_time = time.monotonic()
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
                    dns = ns2 - self.prev[pid][1]
                    if dns > 0:
                        rows.append({"pid": pid, "name": name,
                                     "gpu_ms_s": dns / 1e6 / elapsed})
        self.prev = snap
        self.prev_time = now
        rows.sort(key=lambda r: r["gpu_ms_s"], reverse=True)
        return rows


def render_procs(rows, limit=10):
    """Render a per-process GPU table (like nvidia-smi's process list)."""
    lines = [f"{HEADER} GPU processes (per-process GPU ms/s){RESET}"]
    if not rows:
        lines.append("  \x1b[2m(no GPU activity)\x1b[0m")
        return lines
    lines.append(f"  {'PID':>7}  {'GPU ms/s':>9}  {'%':>5}  NAME")
    for r in rows[:limit]:
        pct = min(100.0, r["gpu_ms_s"] / 10.0)   # 1000 ms/s == 100%
        lines.append(f"  {r['pid']:>7}  {r['gpu_ms_s']:>9.1f}  {pct:>5.1f}  {r['name']}")
    return lines


# --- Memory stats (mach host_statistics64 + sysctl, no sudo) -----------------
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
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


def cluster_type(name):
    """Classify a core name into a cluster kind.

    ECPU000 / Efficiency0 -> efficiency, PCPU010 / Performance0 -> performance.
    Unknown names fall back to 'other' so nothing is ever dropped.
    """
    u = (name or "").upper()
    if u.startswith("E"):
        return ("E", "CPU E-cluster")
    if u.startswith("P"):
        return ("P", "CPU P-cluster")
    return ("?", "CPU other")


def organize(raw):
    """Turn the Sampler's raw channel lists into a render-ready structure.

    Keeps render free of any subgroup names or core-prefix knowledge.
    Returns: {"gpu_pct", "gpu_channels", "clusters":[{"key","label","avg","cores"}]}
    """
    tables = DVFS or {}

    # --- GPU: average across channels if there is more than one ---
    gpu_ch = raw.get("gpu", [])
    gpu_pct = (sum(e["active"] for e in gpu_ch) / len(gpu_ch) * 100) if gpu_ch else 0.0
    gpu_mhz, gpu_unit = cluster_freq(gpu_ch, tables.get("GPU", []))

    # --- CPU: group only the per-core (state residency) channels by cluster ---
    cores = [e for e in raw.get("cpu", []) if "COMPLEX" not in (e["subgroup"] or "").upper()]

    clusters = {}   # key -> {"label", "cores"}
    order = []
    for e in cores:
        key, label = cluster_type(e["name"])
        if key not in clusters:
            clusters[key] = {"key": key, "label": label, "cores": []}
            order.append(key)
        clusters[key]["cores"].append(e)

    out_clusters = []
    for key in order:
        c = clusters[key]
        n = len(c["cores"])
        avg = sum(x["active"] for x in c["cores"]) / n * 100
        mhz, unit = cluster_freq(c["cores"], tables.get(key, []))
        out_clusters.append({**c, "avg": avg, "count": n, "mhz": mhz, "freq_unit": unit})

    return {"gpu_pct": gpu_pct, "gpu_channels": gpu_ch, "gpu_mhz": gpu_mhz,
            "gpu_freq_unit": gpu_unit,
            "clusters": out_clusters,
            "power": raw.get("power", {}), "power_max": raw.get("power_max", {}),
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

# 1/8-step blocks that fill from bottom to top
EIGHTHS = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


def color_for(pct):
    if pct >= 80:
        return "\x1b[91m"  # red
    if pct >= 50:
        return "\x1b[93m"  # yellow
    return "\x1b[92m"      # green


def bar(pct, width=40):
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    reset = "\x1b[0m"
    c = color_for(pct)
    return f"{c}{'█' * filled}{reset}{'─' * (width - filled)}"


def gauge_line(label, pct, width=40):
    return f"  {label:<18} {bar(pct, width)} {pct:6.2f}%"


def bar_frac(frac, width=40):
    """Colored bar for an arbitrary 0..1 fraction."""
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    c = color_for(frac * 100)
    return f"{c}{'█' * filled}{RESET}{'─' * (width - filled)}"


def power_line(label, watts, max_watts, width=40):
    frac = 0.0 if max_watts <= 0 else watts / max_watts
    return f"  {label:<18} {bar_frac(frac, width)} {watts:6.2f}W"


def vgraph(history, height=8, width=48, label_step=50, label_max=None, label_unit="%",
           color=None):
    """Draw history(%) as a vertical bar graph that grows from bottom to top.

    Oldest value on the left, newest on the right; column height is utilization.
    Only y-axis rows whose value is a multiple of label_step are labelled.
    If label_max is given, axis labels show that scale (e.g. watts) instead of %,
    where the top of the graph == label_max.
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
        # y-axis label only at multiples of label_step (e.g. 50%), else blank
        axis = int(round((level + 1) / height * 100))
        if label_step and axis % label_step == 0:
            if label_max is not None:
                lab = f"{(axis / 100) * label_max:.0f}{label_unit}"
            else:
                lab = f"{axis}{label_unit}"
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
    """asitop-style bar: bright '▏' ticks over a dim full-width '▏' track."""
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    c = color_for(frac * 100)
    return f"{c}{'▏' * filled}{TRACK}{'▏' * (width - filled)}{RESET}"


def hgauge(label, frac, width, value=""):
    """asitop-style gauge: a title line, then a filled gauge bar.

    Returns a list of two strings.
    """
    title = f"  {label}" + (f"   {value}" if value else "")
    return [title, f"  [{gauge_bar(frac, width)}]"]


_ANSI_RE = None


def _ansi_re():
    import re
    global _ANSI_RE
    if _ANSI_RE is None:
        _ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
    return _ANSI_RE


def _visible_len(s):
    return len(_ansi_re().sub("", s))


def _truncate_visible(s, width):
    """Cut s to `width` visible columns, keeping ANSI escapes intact.

    Escape sequences cost no width, so they are copied through; a trailing RESET
    is appended if anything was dropped, otherwise the color would bleed on.
    """
    if _visible_len(s) <= width:
        return s
    out, shown, pos = [], 0, 0
    for m in _ansi_re().finditer(s):
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


def _freq_txt(value, unit):
    """'@ 1398 MHz' for a true clock, '@ 62% DVFS' for a scale-less ladder."""
    if not value:
        return ""
    if unit == "MHz":
        return f"@ {value:.0f} MHz"
    return f"@ {value:.0f}% DVFS"


def render(view, cols=80, gpu_hist=None, procs=None, height=None, soc_hist=None,
           process_only=False, single_sample=False):
    """Draw the organize() result (view). Does no data-structure reasoning.

    ``single_sample`` suppresses the avg/peak columns, which carry no information
    when only one sample has been taken (they would all just equal the current).
    """
    width = bar_width_for(cols)
    lines = []

    # Box title: app name + machine name + thermal/throttle state.
    title = f"Soltop · {machine_name()}"
    ts = thermal_state()
    if ts >= 0:
        tcolor = {0: "\x1b[92m", 1: "\x1b[93m", 2: "\x1b[91m", 3: "\x1b[91m"}.get(ts, "")
        tname = THERMAL_NAMES.get(ts, "?")
        thr = " throttling" if ts >= 1 else ""
        title += f"   thermal: {tcolor}{tname}{thr}{RESET}"

    if process_only:
        title = "Soltop · GPU Processes · p: dashboard · q: quit"
        limit = max(1, height - 4) if height else 10
        lines.extend(render_procs(procs or [], limit=limit))
        if height:
            content_height = max(0, height - 2)
            del lines[content_height:]
            while len(lines) < content_height:
                lines.append("")
        return ("\x1b[K\n").join(wrap_box(lines, cols, title)) + "\x1b[K"

    title += "   p: processes   q: quit"

    freq_txt = _freq_txt(view.get("gpu_mhz", 0.0), view.get("gpu_freq_unit", "MHz"))
    cur = view["gpu_pct"]
    if single_sample:
        stats = ""
    elif gpu_hist:
        stats = f"  (avg {sum(gpu_hist) / len(gpu_hist):.1f}%  peak {max(gpu_hist):.1f}%)"
    else:
        stats = ""
    lines.append(f"{HEADER} GPU Usage: {cur:.1f}%{stats}  {freq_txt}{RESET}")
    if gpu_hist is not None:
        lines.extend(vgraph(gpu_hist, height=5, width=max(10, width - 7)))
    lines.append("")

    # Power: cur/avg/peak table for components; total as an asitop-style gauge.
    power = view.get("power", {})
    pmax = view.get("power_max", {})
    pavg = view.get("power_avg", {})
    ppeak = view.get("power_peak", {})
    if power:
        # Components on one line, each with W unit and cur/avg/peak values.
        def triple(lbl):
            if single_sample:
                return f"{lbl} {power[lbl] / 1000:.1f}W"
            return (f"{lbl} {power[lbl] / 1000:.1f}/"
                    f"{pavg.get(lbl, 0) / 1000:.1f}/{ppeak.get(lbl, 0) / 1000:.1f}W")
        comp = " | ".join(triple(lbl) for lbl in ("CPU", "GPU", "ANE", "DRAM") if lbl in power)
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
            lines.extend(vgraph(norm, height=5, width=max(10, width - 7),
                                label_max=scale, label_unit="W", color="\x1b[92m"))
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
        ftxt = _freq_txt(c.get("mhz", 0.0), c.get("freq_unit", "MHz"))
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

    # Fit the complete frame to the available screen height. On very short
    # terminals, lower-priority content at the bottom is omitted.
    if height:
        content_height = max(0, height - 2)
        del lines[content_height:]
        while len(lines) < content_height:
            lines.append("")

    # Draw a full outer border with the machine name + thermal as the title.
    boxed = wrap_box(lines, cols, title)

    # Clear each line end (ESC[K) to remove longer leftovers from the prior frame.
    return ("\x1b[K\n").join(boxed) + "\x1b[K"


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
    try:
        with KeyReader(sys.stdin) as keys:
            while True:
                view = organize(sampler.read(interval))
                gpu_hist.append(view["gpu_pct"])
                soc_hist.append(view.get("power", {}).get("SoC", 0) / 1000)
                try:
                    procs = proc_sampler.step(interval)
                except Exception:
                    procs = []
                # Handle each key in order: every 'p' toggles, 'q'/ESC quits.
                for k in keys.read_available().lower():
                    if k == "p":
                        process_only = not process_only
                    elif k in ("q", "\x1b", "\x03"):
                        return
                tcols, rows = term_size()
                cols = cols_override or tcols
                # On resize, wipe the screen once so wrapped/old lines don't linger.
                if (cols, rows) != last_size:
                    print(CLEAR, end="")
                    last_size = (cols, rows)
                frame = render(view, cols, gpu_hist, procs, height=rows,
                               soc_hist=soc_hist, process_only=process_only)
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


if __name__ == "__main__":
    main()
