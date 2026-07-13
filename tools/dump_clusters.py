#!/usr/bin/env python3
"""Print soltop's cluster grouping next to each core's raw residency.

Diagnostic for "a cluster reads 0%": it distinguishes a cluster that is genuinely
parked (its cores really do sit in DOWN/IDLE) from one that soltop has grouped or
weighted wrongly (its cores are busy but the cluster still averages 0%).

Run it while the machine is BUSY -- an idle cluster and a broken one look the
same otherwise:

    python3 tools/dump_clusters.py

No sudo.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import soltop  # noqa: E402


def main():
    from soltop.core import dvfs as _dvfs
    _dvfs.set_tables(soltop.load_dvfs())
    tables = _dvfs.tables()
    print("decoded voltage-states tables:")
    for key, (kind, ladder) in sorted(tables.items()):
        print(f"  {key:24s} {kind:8s} {len(ladder):2d} steps  "
              f"{ladder[0]:.0f}..{ladder[-1]:.0f} MHz")

    s = soltop.Sampler()
    s.read(0.3)                      # prime the deltas
    raw = s.read(1.0)

    cores = [e for e in raw.get("cpu", [])
             if "COMPLEX" not in (e.get("subgroup") or "").upper()]

    print("\nclusters as soltop groups them:")
    for c in soltop.group_clusters(cores):
        nsteps = soltop._nsteps(c["cores"])
        ladder = soltop.match_cpu_ladder(nsteps, tables)
        avg = (sum(x["active"] for x in c["cores"]) / len(c["cores"]) * 100)
        mhz = soltop.cluster_freq_mhz(c["cores"], ladder)
        print(f"\n  {c['key']:3s} {c['label']:16s} cores={len(c['cores'])} "
              f"nsteps={nsteps:2d} ladder={len(ladder)} steps "
              f"avg={avg:5.1f}%  mhz={mhz:.0f}")
        if not ladder:
            print("      !! no ladder bound -- no table has this step count")
        for e in c["cores"]:
            live = {k: v for k, v in (e.get("states") or {}).items() if v}
            ratio = soltop.active_ratio(e.get("states") or {})
            print(f"      {e['name']:9s} active={e['active'] * 100:5.1f}%  "
                  f"active_ratio={ratio}  residency={live}")

    gpu = raw.get("gpu", [])
    if gpu:
        gsteps = soltop._nsteps(gpu)
        gladder = soltop.match_gpu_ladder(tables)
        top = f"{gladder[-1]:.0f}" if gladder else "-"
        print(f"\n  GPU nsteps={gsteps} ladder={len(gladder)} steps (top {top} MHz)  "
              f"mhz={soltop.cluster_freq_mhz(gpu, gladder):.0f}")


if __name__ == "__main__":
    main()
