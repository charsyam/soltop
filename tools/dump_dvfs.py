#!/usr/bin/env python3
"""Dump every voltage-states* key in the IORegistry, raw.

Diagnostic for calibrating soltop's DVFS tables on a new SoC. Prints the key
names it finds, which IORegistry node they hang off, and the raw uint32 pairs,
plus both candidate interpretations (Hz and period) so the ladder can be
matched against `sudo powermetrics --samplers cpu_power` by eye.

No sudo. Reads nothing but IORegistry properties.
"""
import ctypes
import ctypes.util
import re
import subprocess
import sys
from ctypes import c_void_p, c_char_p, c_uint32, c_int64, c_long, byref, create_string_buffer

CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
IOKIT = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")

kCFStringEncodingUTF8 = 0x08000100
kIORegistryIterateRecursively = 1

CF.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
CF.CFStringCreateWithCString.restype = c_void_p
CF.CFRelease.argtypes = [c_void_p]
CF.CFRelease.restype = None
CF.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_int64, c_uint32]
CF.CFStringGetCString.restype = ctypes.c_bool
CF.CFDataGetLength.argtypes = [c_void_p]
CF.CFDataGetLength.restype = c_long
CF.CFDataGetBytePtr.argtypes = [c_void_p]
CF.CFDataGetBytePtr.restype = c_void_p
CF.CFDictionaryGetCount.argtypes = [c_void_p]
CF.CFDictionaryGetCount.restype = c_long
CF.CFDictionaryGetKeysAndValues.argtypes = [c_void_p, c_void_p, c_void_p]
CF.CFDictionaryGetKeysAndValues.restype = None
CF.CFGetTypeID.argtypes = [c_void_p]
CF.CFGetTypeID.restype = c_long
CF.CFDataGetTypeID.restype = c_long

IOKIT.IORegistryCreateIterator.argtypes = [c_uint32, c_char_p, c_uint32, c_void_p]
IOKIT.IOIteratorNext.argtypes = [c_uint32]
IOKIT.IOIteratorNext.restype = c_uint32
IOKIT.IORegistryEntryCreateCFProperties.argtypes = [c_uint32, c_void_p, c_void_p, c_uint32]
IOKIT.IORegistryEntryGetName.argtypes = [c_uint32, c_char_p]
IOKIT.IOObjectRelease.argtypes = [c_uint32]


def from_cfstr(ref):
    buf = create_string_buffer(512)
    if not CF.CFStringGetCString(ref, buf, len(buf), kCFStringEncodingUTF8):
        return None
    return buf.value.decode("utf-8", "replace")


def entry_name(o):
    buf = create_string_buffer(128)
    if IOKIT.IORegistryEntryGetName(o, buf) != 0:
        return "?"
    return buf.value.decode("utf-8", "replace")


def dump():
    data_type = CF.CFDataGetTypeID()
    it = c_uint32(0)
    if IOKIT.IORegistryCreateIterator(0, b"IOService", kIORegistryIterateRecursively, byref(it)) != 0:
        print("could not open IORegistry iterator", file=sys.stderr)
        return
    found = []
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
                        name = from_cfstr(k) or ""
                        if not re.match(r"^voltage-states", name):
                            continue
                        if not v or CF.CFGetTypeID(v) != data_type:
                            continue
                        nbytes = CF.CFDataGetLength(v)
                        if nbytes < 4:
                            continue
                        raw = list((c_uint32 * (nbytes // 4)).from_address(CF.CFDataGetBytePtr(v)))
                        found.append((name, entry_name(o), raw))
                CF.CFRelease(props)
            IOKIT.IOObjectRelease(o)
    finally:
        IOKIT.IOObjectRelease(it)

    if not found:
        print("no voltage-states* keys found")
        return

    NUMERATOR = 65_532_288  # the M4 Pro timebase soltop currently hardcodes
    for name, node, raw in sorted(found):
        print(f"\n=== {name}   (node: {node})")
        print(f"    {len(raw)} uint32, {len(raw)//2} (freq, volt) pairs")
        print(f"    raw: {raw}")
        first = [x for x in raw[0::2] if x]
        if not first:
            print("    (no nonzero values in the first slot of each pair)")
            continue
        print(f"    first-of-pair: {first}")
        as_hz = sorted(round(v / 1e6, 1) for v in first)
        as_period = sorted(round(NUMERATOR / v, 1) for v in first)
        print(f"    if Hz     -> MHz: {as_hz}")
        print(f"    if period -> MHz: {as_period}   (using M4 numerator {NUMERATOR})")


def hw():
    for cmd in (["sysctl", "-n", "machdep.cpu.brand_string"],
                ["sysctl", "-n", "hw.perflevel0.physicalcpu"],
                ["sysctl", "-n", "hw.perflevel1.physicalcpu"],
                ["sysctl", "-n", "hw.nperflevels"],
                ["sysctl", "-n", "hw.tbfrequency"],
                ["sysctl", "-n", "kern.clockrate"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            out = "(failed)"
        print(f"{cmd[-1]:35s} {out}")


def channels():
    """Print the IOReport CPU/GPU channel names and their P-state names.

    This is what soltop actually groups cores by -- NOT the powermetrics cluster
    labels. The core-name format ('PCPU000', 'ECPU010', ...) is what decides the
    cluster split, and it is not safe to assume it carries across chips.
    """
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    import soltop

    s = soltop.Sampler()
    raw = s.read(0.5)
    for kind in ("cpu", "gpu"):
        for e in raw.get(kind, []):
            names = list(e.get("states") or {})
            nactive = sum(1 for n in names if not soltop.is_idle_state(n))
            print(f"  {kind}  name={e['name']!r}")
            print(f"        group={e.get('group')!r}  subgroup={e.get('subgroup')!r}")
            print(f"        {len(names)} states ({nactive} non-idle): {names}")


if __name__ == "__main__":
    print("--- hardware -----------------------------------------------")
    hw()
    print("\n--- ioreport channels --------------------------------------")
    try:
        channels()
    except Exception as e:
        print(f"  (failed: {e})")
    print("\n--- voltage-states -----------------------------------------")
    dump()
