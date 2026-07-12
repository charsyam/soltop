# soltop

Current version: **0.4.1**

An Apple Silicon GPU / CPU / power monitor for the terminal — like `asitop`,
but **without `sudo` and without `powermetrics`**.

`soltop` reads directly from `IOReport`, IOKit and mach, so it runs with normal
user privileges.

## Features

- **GPU** usage + frequency, with a live history graph
- **Per-process GPU usage** (like `nvidia-smi`), read from the driver's
  IORegistry accounting — no sudo
- **Power**: CPU / GPU / ANE / DRAM / Total (cur / avg / peak) + history graph
- **CPU** E/P clusters: usage + DVFS level, with core counts
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

While running, press `p` to toggle between the dashboard and the full GPU
process list. Press `q` (or `Ctrl-C`) to quit.

## Requirements

- Apple Silicon Mac (M1 or newer)
- macOS with the system `python3`

## Notes / accuracy

- No `sudo` and no `powermetrics` dependency.
- GPU utilization and power come from IOReport residency / energy counters;
  they track trends well but are approximations, not firmware-exact values.
- **GPU frequency is exact MHz.** CPU clusters are reported as a *DVFS level*
  (`@ 62% DVFS`) rather than MHz: the CPU `voltage-states` table uses a raw unit
  with no documented MHz conversion, and it varies by generation. Earlier
  versions normalized it against a hardcoded per-cluster maximum, which produced
  confidently wrong clock numbers on any chip that didn't match. A percentage of
  the cluster's own top DVFS step is what the data actually supports.

## License

MIT — see [LICENSE](LICENSE).
