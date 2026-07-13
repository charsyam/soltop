"""Raw ctypes bindings: CoreFoundation, IOReport, IOKit, libc.

Declarations and the two CFString helpers -- nothing that interprets a value.
Every layer above talks to Apple's C APIs through these symbols.
"""
import ctypes
import ctypes.util
from ctypes import (c_void_p, c_char_p, c_int, c_uint32, c_uint64, c_int64,
                    c_long, POINTER, byref, create_string_buffer)


#!/usr/bin/env python3
"""Soltop — read GPU utilization and CPU cluster residency via IOReport.

Computes GPU/CPU active utilization from P-State residency on Apple Silicon.
"""



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
