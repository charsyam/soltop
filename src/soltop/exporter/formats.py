"""Machine-readable output: JSON, CSV, Prometheus.

One snapshot() feeds all three, so the formats cannot drift apart.

An unknown clock is null/absent, NEVER 0: a parked cluster and unreadable
silicon both have no frequency, and a zero averages cleanly -- it would drag a
dashboard quietly towards nothing.
"""
import json

from .. import __version__
from ..core.system import THERMAL_NAMES, machine_name, thermal_state


def snapshot(view, timestamp):
    """A flat, serialisable view: the one schema JSON/CSV/Prometheus share.

    ``timestamp`` is passed in rather than read here so the caller controls the
    clock (and so this stays a pure function of the view).
    """
    def mhz(v):
        return round(v) if v else None       # never 0 -- see above

    def best_effort(fn, default=None):
        # An exporter is a long-lived process: a failure to read a decorative
        # field (the machine's name, the thermal state) must not take the whole
        # metric stream down with it.
        try:
            return fn()
        except Exception:
            return default

    return {
        "timestamp": timestamp,
        "soltop_version": __version__,
        "machine": best_effort(machine_name),
        "thermal": best_effort(lambda: THERMAL_NAMES.get(thermal_state())),
        "gpu": {
            "utilization_percent": round(view.get("gpu_pct", 0.0), 2),
            "frequency_mhz": mhz(view.get("gpu_mhz")),
        },
        "cpu_clusters": [
            {
                "cluster": c["key"],
                "cores": c["count"],
                "utilization_percent": round(c["avg"], 2),
                "frequency_mhz": mhz(c["mhz"]),
                # A cluster macOS has powered down: 0% and no clock, by design.
                "parked": c["avg"] == 0.0 and not c["mhz"],
            }
            for c in view.get("clusters", [])
        ],
        "power_mw": {k.lower(): round(v, 1)
                     for k, v in sorted(view.get("power", {}).items())},
        # SoC die, NOT a GPU temperature -- see core/temps.py. Absent (not 0)
        # when no die sensor can be read.
        "soc_temp_celsius": ({k: round(v, 1) for k, v in view["soc_temp"].items()}
                             if view.get("soc_temp") else None),
    }


def to_json(snap):
    """One snapshot as a single JSON line (JSONL: stream-friendly, jq-friendly)."""
    import json
    return json.dumps(snap, separators=(",", ":"))


def _csv_columns(snap):
    """CSV header for this machine's shape.

    The cluster set is chip-specific (an M4 Pro has E/P0/P1, an M5 Pro S/P0/P1),
    so the columns are derived from the snapshot rather than fixed. Within one
    run they are stable, which is what a CSV consumer actually needs.
    """
    cols = ["timestamp", "gpu_utilization_percent", "gpu_frequency_mhz"]
    for c in snap["cpu_clusters"]:
        cols += [f"cpu_{c['cluster']}_utilization_percent",
                 f"cpu_{c['cluster']}_frequency_mhz"]
    cols += [f"power_{rail}_mw" for rail in snap["power_mw"]]
    cols += ["soc_temp_max_celsius", "soc_temp_avg_celsius"]
    return cols


def to_csv_row(snap):
    """One snapshot as a CSV row (an empty field where a value is unknown)."""
    def f(v):
        return "" if v is None else v

    row = [snap["timestamp"],
           snap["gpu"]["utilization_percent"], f(snap["gpu"]["frequency_mhz"])]
    for c in snap["cpu_clusters"]:
        row += [c["utilization_percent"], f(c["frequency_mhz"])]
    row += [v for v in snap["power_mw"].values()]
    t = snap["soc_temp_celsius"] or {}
    row += [f(t.get("max")), f(t.get("avg"))]
    return ",".join(str(x) for x in row)


def to_prometheus(snap):
    """One snapshot in the Prometheus text exposition format.

    A metric whose value is unknown is OMITTED rather than exported as 0 -- an
    absent series is honest, a zeroed one is a lie that averages cleanly.
    """
    out = []

    def metric(name, help_text, mtype, samples):
        # Only emit the HELP/TYPE preamble if at least one sample survived.
        rows = [(labels, val) for labels, val in samples if val is not None]
        if not rows:
            return
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {mtype}")
        for labels, val in rows:
            lbl = ("{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"
                   ) if labels else ""
            out.append(f"{name}{lbl} {val}")

    metric("soltop_gpu_utilization_percent", "GPU active residency.", "gauge",
           [({}, snap["gpu"]["utilization_percent"])])
    metric("soltop_gpu_frequency_mhz", "GPU active-residency-weighted clock.",
           "gauge", [({}, snap["gpu"]["frequency_mhz"])])
    metric("soltop_cpu_utilization_percent", "CPU cluster active residency.",
           "gauge", [({"cluster": c["cluster"]}, c["utilization_percent"])
                     for c in snap["cpu_clusters"]])
    metric("soltop_cpu_frequency_mhz",
           "CPU cluster active-residency-weighted clock.", "gauge",
           [({"cluster": c["cluster"]}, c["frequency_mhz"])
            for c in snap["cpu_clusters"]])
    metric("soltop_cpu_cores", "Cores in the cluster.", "gauge",
           [({"cluster": c["cluster"]}, c["cores"]) for c in snap["cpu_clusters"]])
    metric("soltop_power_milliwatts", "Power draw by rail.", "gauge",
           [({"rail": rail}, v) for rail, v in snap["power_mw"].items()])
    temp = snap["soc_temp_celsius"] or {}
    metric("soltop_soc_temperature_celsius",
           "SoC die temperature. NOT a GPU temperature -- no GPU-specific "
           "sensor is exposed; see core/temps.py.", "gauge",
           [({"stat": "max"}, temp.get("max")), ({"stat": "avg"}, temp.get("avg"))])
    metric("soltop_build_info", "soltop version and machine.", "gauge",
           [({"version": snap["soltop_version"],
              "machine": snap["machine"] or "unknown"}, 1)])
    return "\n".join(out) + "\n"
