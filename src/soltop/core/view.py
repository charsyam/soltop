"""Assemble the render-ready view from the raw sample.

Nothing here interprets hardware -- cpu.py, gpu.py and power.py each own their
domain, and this only stitches their results into the one dict that the display
and the exporters consume.
"""

from .cpu import cpu_view
from .gpu import gpu_view


def organize(raw):
    """Turn the Sampler's raw channel lists into a render-ready structure.

    Keeps render() free of any subgroup names or core-prefix knowledge.
    """
    gpu = gpu_view(raw)
    return {
        "gpu_pct": gpu["pct"], "gpu_mhz": gpu["mhz"], "gpu_channels": gpu["channels"],
        "clusters": cpu_view(raw),
        "power": raw.get("power", {}),
        "power_avg": raw.get("power_avg", {}),
        "power_peak": raw.get("power_peak", {}),
    }
