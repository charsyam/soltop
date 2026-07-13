"""Machine facts that are not IOReport: memory, model name, thermal state.

All read without sudo -- mach host_statistics64, sysctl, and NSProcessInfo.
"""
import ctypes
import ctypes.util
import subprocess
from ctypes import (c_void_p, c_int, c_uint32, c_uint64, c_long, c_char_p,
                    byref, create_string_buffer, Structure, POINTER)

from ..ffi import _libc


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
