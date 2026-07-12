# soltop

Current version: **0.7.2**

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

## Requirements

- Apple Silicon Mac (M1 or newer)
- macOS with the system `python3`

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
  `sudo powermetrics --samplers cpu_power`: every step of both CPU ladders it
  prints is reproduced exactly.
- The reported clock is the **active-residency-weighted** one — `powermetrics`'
  "HW active frequency", i.e. the clock a core runs at while it is actually
  running, with idle excluded.

## License

MIT — see [LICENSE](LICENSE).
