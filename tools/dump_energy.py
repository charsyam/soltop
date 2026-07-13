#!/usr/bin/env python3
"""Dump the Energy Model channels, and test whether power can be derived
without hardcoding channel names.

soltop currently hardcodes ENERGY_KEYS = {"CPU Energy", "GPU", "ANE", "DRAM"}.
That is the same pattern that broke the DVFS tables on an M5 -- a name that
means one thing on one chip and something else (or nothing) on the next. This
script checks the two properties that would let the names be *derived* instead:

  1. HIERARCHY. The per-cluster channels (PACC0_CPU, PACC1_CPU, EACC_CPU, ...)
     should sum to the aggregate 'CPU Energy'. If they do, CPU power can be had
     by discovering and summing them -- which also picks up an M5's three
     clusters automatically -- and cross-checked against the aggregate.

  2. UNITS. Some channels are in different units: on an M4 Pro 'GPU Energy' is
     ~1e6 x 'GPU', i.e. one is uJ and the other mJ. Picking the wrong one yields
     a plausible-looking channel reporting tens of kilowatts. A SoC in a laptop
     cannot exceed ~100 W, so the resulting wattage is itself the check.

Compare the output against:

    sudo powermetrics --samplers cpu_power -i 1000 -n 2 | grep -E "Power"

No sudo needed for this script.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import soltop  # noqa: E402
from soltop.core import sampler as _sampler  # noqa: E402

# A laptop/desktop SoC package cannot plausibly draw more than this.
SANE_MAX_W = 200.0

# Per-cluster CPU energy accumulators: EACC_CPU, PACC0_CPU, PACC1_CPU, ...
# The digit is the cluster index; the bare form has no per-core suffix.
CLUSTER_RE = re.compile(r"^([EPS])ACC(\d*)_CPU$")


def sample(seconds=1.0):
    """Return {channel_name: delta} for the Energy Model group."""
    captured = {}
    orig = _sampler.iter_channels

    def spy(delta):
        for ch in orig(delta):
            group = soltop.from_cfstr(soltop.IOR.IOReportChannelGetGroup(ch))
            if group == "Energy Model":
                name = soltop.from_cfstr(soltop.IOR.IOReportChannelGetChannelName(ch))
                captured[name] = soltop.IOR.IOReportSimpleGetIntegerValue(ch, 0)
            yield ch

    s = soltop.Sampler()
    _sampler.iter_channels = spy
    try:
        s.read(0.3)          # prime
        captured.clear()
        s.read(seconds)
    finally:
        _sampler.iter_channels = orig
        s.close()
    return captured, seconds


def main():
    chans, secs = sample()
    if not chans:
        print("no Energy Model channels found")
        return

    def mW(name):
        """Interpret a channel's delta as mJ over the interval -> mW."""
        return chans.get(name, 0) / secs

    print(f"sampled {len(chans)} Energy Model channels over {secs:.1f}s\n")

    # --- 1. Does the per-cluster hierarchy sum to the aggregate? -------------
    clusters = {n: v for n, v in chans.items() if CLUSTER_RE.match(n)}
    agg = chans.get("CPU Energy", 0)
    total = sum(clusters.values())
    print("CPU, by cluster (this is what soltop could DISCOVER rather than name):")
    for n in sorted(clusters):
        print(f"   {n:14s} {clusters[n] / secs:9.1f} mW")
    print(f"   {'sum':14s} {total / secs:9.1f} mW")
    print(f"   {'CPU Energy':14s} {agg / secs:9.1f} mW   (the aggregate soltop uses today)")
    if agg:
        ratio = total / agg
        ok = "OK -- the hierarchy holds" if 0.9 <= ratio <= 1.1 else "MISMATCH"
        print(f"   sum / aggregate = {ratio:.3f}   <- {ok}")
    else:
        print("   !! no 'CPU Energy' channel on this chip")

    # --- 2. Which channels are in which units? ------------------------------
    print("\nevery channel, read as mJ -> mW. A value far above a plausible SoC")
    print(f"budget ({SANE_MAX_W:.0f} W) is NOT in mJ, and must not be used as power:")
    for name in sorted(chans, key=lambda n: -chans[n]):
        w = mW(name) / 1000.0
        if chans[name] == 0:
            continue
        flag = "  <-- IMPLAUSIBLE (different unit)" if w > SANE_MAX_W else ""
        print(f"   {name:22s} {mW(name):12.1f} mW  = {w:9.3f} W{flag}")

    # --- 3. What soltop reports today, for comparison ------------------------
    print("\nsoltop's current hardcoded picks:")
    for name, label in _sampler.ENERGY_KEYS.items():
        present = "present" if name in chans else "ABSENT ON THIS CHIP"
        print(f"   {label:5s} <- {name!r:14s} {mW(name):10.1f} mW   ({present})")


if __name__ == "__main__":
    main()
