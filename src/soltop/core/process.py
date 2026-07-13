"""Per-process GPU usage, from the driver's IORegistry accounting.

The GPU driver publishes accumulatedGPUTime per client; diffing it over an
interval gives per-process GPU busy time -- no sudo, no powermetrics.
"""
import re
import subprocess
import time
from ctypes import c_void_p, c_uint32, c_int64, byref

from ..ffi import (CF, IOKIT, cfstr, from_cfstr, kCFNumberSInt64Type,
                   kIORegistryIterateRecursively)


# GPU client user-client class. Falls back through generations if renamed.
GPU_CLIENT_CLASSES = (b"AGXDeviceUserClient", b"IOGPUDeviceUserClient")

# Cache CFString keys once (creating them per lookup would leak a CFString each).


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
