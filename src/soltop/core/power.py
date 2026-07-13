"""Power: which Energy Model channels to read, and how to sanity-check them.

The aggregate channel names turn out to be the stable abstraction across chips
(verified on an M4 Pro and an M5 Pro), while the per-cluster accumulators
beneath them are renamed wholesale. See tools/fixtures/m5pro-energy-model.txt.
"""


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
ENERGY_KEYS = {"CPU Energy": "CPU", "GPU": "GPU", "ANE": "ANE", "DRAM": "DRAM"}


POWER_LABELS = ("CPU", "GPU", "ANE", "DRAM")

# Not every Energy Model channel counts in mJ: 'GPU Energy' is in uJ, and reading
# it as mJ yields 272 kW on an M4 Pro and 2833 W on an M5 Pro. We do not use that
# channel today, but a future chip that drops the one we do use ('GPU') would
# silently render a four-digit wattage rather than fail. No Apple SoC in a Mac
# draws anywhere near this, so a reading above it is a unit mismatch, not power:
# treat the channel as unreadable and show nothing.


# Not every Energy Model channel counts in mJ: 'GPU Energy' is in uJ, and reading
# it as mJ yields 272 kW on an M4 Pro and 2833 W on an M5 Pro. We do not use that
# channel today, but a future chip that drops the one we do use ('GPU') would
# silently render a four-digit wattage rather than fail. No Apple SoC in a Mac
# draws anywhere near this, so a reading above it is a unit mismatch, not power:
# treat the channel as unreadable and show nothing.
POWER_SANE_MAX_MW = 200_000.0    # 200 W


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
