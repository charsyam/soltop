"""GPU: turn the raw GPU residency channels into a utilization and a clock."""

from . import dvfs as _dvfs
from .dvfs import cluster_freq_mhz, match_gpu_ladder


def gpu_view(raw, tables=None):
    """{"pct", "mhz", "channels"} from the Sampler's raw GPU channel list.

    Several GPU channels are averaged; the clock is the active-residency-weighted
    one. A GPU whose ladder cannot be identified gets 0.0 here, which the display
    and the exporters both render as "no clock" rather than as a real zero.
    """
    tables = _dvfs.tables() if tables is None else (tables or {})
    channels = raw.get("gpu", [])
    pct = (sum(e["active"] for e in channels) / len(channels) * 100) if channels else 0.0
    mhz = cluster_freq_mhz(channels, match_gpu_ladder(tables))
    return {"pct": pct, "mhz": mhz, "channels": channels}
