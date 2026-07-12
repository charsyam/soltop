# soltop

Current version: **0.6.0**

An Apple Silicon GPU / CPU / power monitor for the terminal — like `asitop`,
but **without `sudo` and without `powermetrics`**.

`soltop` reads directly from `IOReport`, IOKit and mach, so it runs with normal
user privileges.

## Features

- **GPU** usage + frequency, with a live history graph
- **Per-process GPU table** (like `nvidia-smi`), read from the driver's
  IORegistry accounting — no sudo. Shows GPU ms/s and GPU%, plus each process's
  CPU%, memory, and how long ago it last submitted GPU work. Idle GPU clients
  are listed too, so you can see who is still holding a GPU context.
- **Power**: CPU / GPU / ANE / DRAM / Total (cur / avg / peak) + history graph
- **CPU** E/P clusters: usage + DVFS level, with core counts (press `c` for a
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
- **GPU frequency is exact MHz.** CPU clusters are reported as a *DVFS level*
  (`@ 62% DVFS`) rather than MHz: the CPU `voltage-states` table uses a raw unit
  with no documented MHz conversion, and it varies by generation. Earlier
  versions normalized it against a hardcoded per-cluster maximum, which produced
  confidently wrong clock numbers on any chip that didn't match. A percentage of
  the cluster's own top DVFS step is what the data actually supports.
- The reported clock is the **mean over the sampling interval**, with idle
  residency counted at the bottom of the ladder — the same thing `powermetrics`
  reports. It is not "the clock while a core happens to be awake", which on
  Apple Silicon is almost always the top step (a core runs flat out, then drops
  straight to idle) and so would sit near 100% even on an idle machine.

## License

MIT — see [LICENSE](LICENSE).
