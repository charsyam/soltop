"""IOReport: discover the utilization channels, subscribe, and sample.

This layer only *reads* -- it hands out raw per-channel residency and energy
deltas. What those numbers mean is decided in cpu.py / gpu.py / power.py.
"""
import time
from concurrent.futures import ThreadPoolExecutor

from ..ffi import (CF, IOR, c_void_p, c_int, c_uint64, byref, cfstr, from_cfstr)
from . import dvfs as _dvfs
from .dvfs import active_ratio, load_dvfs
from .power import ENERGY_KEYS, POWER_LABELS, POWER_SANE_MAX_MW

FIRST_SAMPLE_MAX_INTERVAL = 0.2


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

# M4/M5 use these canonical group/subgroup pairs.  Try them directly before
# scanning every IOReport channel; a future rename still falls back to the
# discovery path below.
_CANONICAL_UTIL_CHANNELS = (
    ("CPU Stats", "CPU Core Performance States"),
    ("GPU Stats", "GPU Performance States"),
)
# Substrings identifying subgroups that are NOT utilization, used only by the
# fallback scan below if Apple renames the canonical subgroups above.


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


def _canonical_state_channels():
    """Copy the known M4/M5 utilization channels, or None if either is absent."""
    refs = []
    try:
        # These independent calls each take about 0.1s. ctypes releases the GIL,
        # so copying them concurrently cuts the canonical fast path nearly in half.
        with ThreadPoolExecutor(max_workers=len(_CANONICAL_UTIL_CHANNELS)) as pool:
            refs = list(pool.map(lambda pair: copy_group(*pair),
                                 _CANONICAL_UTIL_CHANNELS))
        if any(not ref or not any(iter_channels(ref)) for ref in refs):
            return None
        base = refs[0]
        for ref in refs[1:]:
            IOR.IOReportMergeChannels(base, ref, None)
            CF.CFRelease(ref)
        refs = []
        return base
    finally:
        for ref in refs:
            if ref:
                CF.CFRelease(ref)


# Energy Model channels of interest -> display label. Their delta over the
# interval is energy consumed, so power (mW) = delta / interval_seconds.
# "SoC" is derived as the sum of these components.
#
# These four aggregate names are stable across chips -- verified present and
# correct on both an M4 Pro and an M5 Pro (see tools/fixtures/m5-energy.txt),
# even though the per-cluster channels beneath them are renamed wholesale
# (an M4's PACC0_CPU/EACC_CPU become an M5's MCPU0/PCPU/PACC_0). Deriving power
# by discovering and summing those cluster channels was tried and does NOT
# survive the chip change; the aggregates do.


def build_subscription():
    chans = _canonical_state_channels() or discover_state_channels()
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
        if _dvfs.DVFS is None:
            # DVFS walks the IORegistry while subscription setup reads IOReport.
            # They are independent native operations and ctypes releases the GIL,
            # so overlap them instead of paying both startup costs serially.
            with ThreadPoolExecutor(max_workers=2) as pool:
                future = pool.submit(load_dvfs)
                self.subscribed, self.chans, self.sub = build_subscription()
                try:
                    _dvfs.set_tables(future.result())
                except Exception:
                    _dvfs.set_tables({})
        else:
            self.subscribed, self.chans, self.sub = build_subscription()
        self.closed = False
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
                        mw = max(0.0, energy / elapsed)
                        # A reading above any plausible SoC budget means this
                        # channel is not counting in mJ (see POWER_SANE_MAX_MW).
                        # Report nothing rather than a fabricated four-digit
                        # wattage, the way an unreadable DVFS table reports no
                        # clock.
                        if mw <= POWER_SANE_MAX_MW:
                            current_power[ENERGY_KEYS[name]] = mw
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
