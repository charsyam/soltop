"""SoC die temperature, from the IOHID temperature sensors. No sudo.

WHAT THIS IS NOT: a GPU temperature. This machine exposes no GPU-specific
sensor, and the ones it does expose barely notice the GPU. Pinning the GPU with
a Metal compute kernel on an M4 Pro moves the die sensors by about +1 C, while
pinning the CPU moves the same sensors by +13 C. Other tools look for a sensor
whose *name* contains "GPU" and report 0.0 when (as here) there is none.

So this reports the SoC die honestly, and calls it that. Apple Silicon is one
die -- the GPU's heat does land in these sensors -- but the number is dominated
by the CPU, and labelling it "GPU temperature" would be a fabrication of exactly
the kind the rest of this codebase refuses to make.
"""
import ctypes
import re
from ctypes import byref, c_double, c_int32, c_int64, c_void_p

from ..ffi import CF, IOKIT, cfstr, from_cfstr

# HID sensor matching: page 0xff00 ("Apple vendor"), usage 5 (temperature).
_HID_PAGE = 0xFF00
_HID_USAGE_TEMPERATURE = 5
_EVENT_TYPE_TEMPERATURE = 15
_CFNUMBER_SINT32 = 3

# 'PMU tdie<n>' are the SoC die sensors, and the only ones that track load. The
# rest of what this page reports is not the die:
#   PMU tcal          pinned at 51.82 C under any load -- a calibration constant
#   gas gauge battery the battery pack
#   NAND CH0 temp     the flash
_DIE_RE = re.compile(r"^PMU tdie\d+")

# A reading outside this is not a die temperature (a disconnected sensor reads 0,
# and 150 C is well past where the machine would have shut down).
_SANE_C = (1.0, 150.0)

_client = None          # the IOHIDEventSystemClient, created once
_services = None        # its matching services


def _bind():
    """Declare the IOHID symbols. Idempotent; called on first use."""
    IOKIT.IOHIDEventSystemClientCreate.argtypes = [c_void_p]
    IOKIT.IOHIDEventSystemClientCreate.restype = c_void_p
    IOKIT.IOHIDEventSystemClientSetMatching.argtypes = [c_void_p, c_void_p]
    IOKIT.IOHIDEventSystemClientCopyServices.argtypes = [c_void_p]
    IOKIT.IOHIDEventSystemClientCopyServices.restype = c_void_p
    IOKIT.IOHIDServiceClientCopyProperty.argtypes = [c_void_p, c_void_p]
    IOKIT.IOHIDServiceClientCopyProperty.restype = c_void_p
    IOKIT.IOHIDServiceClientCopyEvent.argtypes = [c_void_p, c_int64, c_int32, c_int64]
    IOKIT.IOHIDServiceClientCopyEvent.restype = c_void_p
    IOKIT.IOHIDEventGetFloatValue.argtypes = [c_void_p, c_int32]
    IOKIT.IOHIDEventGetFloatValue.restype = c_double

    CF.CFDictionaryCreateMutable.argtypes = [c_void_p, c_int64, c_void_p, c_void_p]
    CF.CFDictionaryCreateMutable.restype = c_void_p
    CF.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]
    CF.CFNumberCreate.argtypes = [c_void_p, c_int32, c_void_p]
    CF.CFNumberCreate.restype = c_void_p


def _open():
    """Open the temperature-sensor services once, and cache them."""
    global _client, _services
    if _services is not None:
        return _services
    _bind()
    match = CF.CFDictionaryCreateMutable(None, 0, None, None)
    for key, value in (("PrimaryUsagePage", _HID_PAGE),
                       ("PrimaryUsage", _HID_USAGE_TEMPERATURE)):
        n = c_int32(value)
        CF.CFDictionarySetValue(match, cfstr(key),
                                CF.CFNumberCreate(None, _CFNUMBER_SINT32, byref(n)))
    _client = IOKIT.IOHIDEventSystemClientCreate(None)
    IOKIT.IOHIDEventSystemClientSetMatching(_client, match)
    _services = IOKIT.IOHIDEventSystemClientCopyServices(_client) or 0
    return _services


def die_temps():
    """Every SoC die sensor's reading, in Celsius. [] if none are readable.

    The same physical sensor is published by several HID services, so the same
    name comes back more than once; the caller wants the distribution, not a
    de-duplicated set, and mean/max over the raw readings is the right summary.
    """
    try:
        services = _open()
        if not services:
            return []
        out = []
        lo, hi = _SANE_C
        for i in range(CF.CFArrayGetCount(services)):
            service = CF.CFArrayGetValueAtIndex(services, i)
            name = from_cfstr(IOKIT.IOHIDServiceClientCopyProperty(
                service, cfstr("Product")))
            if not name or not _DIE_RE.match(name):
                continue
            event = IOKIT.IOHIDServiceClientCopyEvent(
                service, _EVENT_TYPE_TEMPERATURE, 0, 0)
            if not event:
                continue
            c = IOKIT.IOHIDEventGetFloatValue(
                event, _EVENT_TYPE_TEMPERATURE << 16)
            if lo <= c <= hi:
                out.append(c)
        return out
    except Exception:
        return []


def soc_temp():
    """{"avg", "max"} degrees C across the die sensors, or {} if unreadable.

    'max' is the one that matters for throttling -- the hottest spot on the die
    is what the thermal governor reacts to -- and 'avg' is the steadier number.
    """
    temps = die_temps()
    if not temps:
        return {}
    return {"avg": sum(temps) / len(temps), "max": max(temps)}
