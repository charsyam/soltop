# soltop

[![test](https://github.com/charsyam/soltop/actions/workflows/test.yml/badge.svg)](https://github.com/charsyam/soltop/actions/workflows/test.yml)

Current version: **0.8.0**

An Apple Silicon GPU / CPU / power monitor for the terminal — like `asitop`,
but **without `sudo` and without `powermetrics`**.

`soltop` reads directly from `IOReport`, IOKit and mach, so it runs with normal
user privileges.

![soltop running on an Apple M4 Pro](soltop.png)

## Features

- **GPU** usage + frequency, with a live history graph
- **Per-process GPU table** (like `nvidia-smi`), read from the driver's
  IORegistry accounting — no sudo. Shows GPU ms/s and GPU%, plus each process's
  CPU% and memory.
- **Power**: CPU / GPU / ANE / DRAM / Total (cur / avg / peak) + history graph
- **CPU** E/P clusters: usage + frequency, with core counts (press `c` for a
  per-core breakdown)
- **Memory**: used / wired / compressed / swap
- **Thermal / throttle** state
- Auto-fits the terminal size, boxed asitop-style UI

## Install

```sh
brew install charsyam/tap/soltop
```

## Usage

```sh
soltop              # live monitor
soltop -i 0.5       # sample every 0.5s
soltop --once       # print one frame and exit
soltop --version
```

While running:

| key | action |
|-----|--------|
| `p` | toggle the full GPU process list |
| `c` | toggle the per-core CPU view (every E/P core individually, instead of the cluster averages) |
| `q` | quit (`Ctrl-C` also works) |

Pressing the same key again returns to the dashboard.

## Machine-readable output

```sh
soltop --json                    # one JSON object per sample (JSONL)
soltop --json --once | jq .      # a single snapshot
soltop --csv > soltop.csv        # a header, then one row per sample
soltop --serve 9101              # Prometheus metrics at :9101/metrics
```

`--serve` binds **loopback** unless you give an address (`--serve 0.0.0.0:9101`) —
exporting hardware telemetry to the network should be deliberate. A background
thread samples continuously and scrapes read the latest snapshot, so a scrape
returns immediately and *N* scrapers cost no more than one.

```
soltop_gpu_utilization_percent 29.4
soltop_gpu_frequency_mhz 618
soltop_cpu_utilization_percent{cluster="P0"} 90.0
soltop_cpu_frequency_mhz{cluster="P0"} 4380
soltop_power_milliwatts{rail="cpu"} 1360.0
```

**An unknown clock is never exported as 0.** A parked cluster (macOS powers whole
CPU clusters down when idle) and a chip whose ladder soltop cannot read both have
*no* frequency — so JSON emits `null`, CSV leaves the field empty, and Prometheus
**omits the series entirely**. A zero would average cleanly and drag a dashboard
quietly towards nothing; an absent series is honest.

## Requirements

- Apple Silicon Mac (M1 or newer)
- macOS with the system `python3`

## Which Macs are verified

Frequencies are calibrated against `sudo powermetrics` on real hardware, and
Apple's IORegistry naming changes between generations — so "it runs" and "the
numbers are right" are different claims. What has actually been checked against
ground truth:

| chip | frequencies | clusters | power |
|---|---|---|---|
| M4 Pro | ✅ verified | ✅ E + P0/P1 | ✅ verified |
| M5 Pro | ✅ verified | ✅ S + P0/P1 | ✅ verified |
| everything else | ⚠️ unverified | ⚠️ unverified | ⚠️ unverified |

soltop is built to **degrade honestly** — on silicon whose tables it cannot
read it shows no clock rather than a wrong one — but that is a design goal, not
a measurement.

**Running an M1/M2/M3, or a Max/Ultra?** Two commands make your chip verified,
and take a minute:

```sh
python3 tools/dump_dvfs.py > mychip.txt
sudo powermetrics --samplers cpu_power -i 1000 -n 2 | grep "HW active frequency"
```

Open an issue with both outputs. That is exactly how M5 Pro support was built —
it turned out to have no efficiency cores at all, and to name its *Super* cores
`PCPU*`, the same prefix an M4 uses for its *performance* cores. No amount of
reasoning would have found that; the dump did, in one shot.

## Notes / accuracy

- No `sudo` and no `powermetrics` dependency.
- GPU utilization and power come from IOReport residency / energy counters;
  they track trends well but are approximations, not firmware-exact values.
- The process table's **MEM** is the process's RSS. Apple Silicon memory is
  unified, so that *is* the memory it costs the SoC — the GPU driver publishes
  no separate VRAM figure (per GPU client it exposes only the API, the
  accumulated GPU time, and the last submission time).
- **Frequencies are exact MHz**, for GPU and CPU alike. The GPU's
  `voltage-states` table holds plain Hz; the CPU's holds the *period* of each
  step, so `MHz = 65532288 / raw`. Verified against
  `sudo powermetrics --samplers cpu_power` on an **M4 Pro and an M5 Pro**: every
  step of every CPU ladder it prints is reproduced exactly.
- The reported clock is the **active-residency-weighted** one — `powermetrics`'
  "HW active frequency", i.e. the clock a core runs at while it is actually
  running, with idle excluded.
- **The cluster layout is discovered, never assumed.** Apple's IORegistry
  naming is not stable across chips: an M5 Pro has no efficiency cores at all
  (5 Super + 10 Performance), keeps its ladders under different
  `voltage-states` keys than an M4, and names its *Super* cores `PCPU*` — the
  same prefix an M4 uses for its *performance* cores. soltop therefore binds
  each cluster to its ladder by matching shape, and ranks the E/P/S tiers by
  their measured ceiling. On silicon it cannot read, it shows **no clock**
  rather than a fabricated one; utilization keeps working regardless. The raw
  captures behind this are in [`tools/fixtures/`](tools/fixtures/).
- A cluster that reads **0% with no clock is parked**, not broken — macOS powers
  whole CPU clusters down when idle.

## License

MIT — see [LICENSE](LICENSE).
