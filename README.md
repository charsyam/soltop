# soltop

Current version: **0.2.0**

An Apple Silicon GPU / CPU / power monitor for the terminal — like `asitop`,
but **without `sudo` and without `powermetrics`**.

`soltop` reads directly from `IOReport`, IOKit and mach, so it runs with normal
user privileges.

## Features

- **GPU** usage + frequency, with a live history graph
- **Per-process GPU usage** (like `nvidia-smi`), read from the driver's
  IORegistry accounting — no sudo
- **Power**: CPU / GPU / ANE / DRAM / Total (cur / avg / peak) + history graph
- **CPU** E/P clusters: usage + approximate frequency, with core counts
- **Memory**: used / wired / compressed / swap
- **Thermal / throttle** state
- Auto-fits the terminal size, boxed asitop-style UI

## Install

```sh
brew install charsyam/tap/soltop
```

## Usage

```sh
soltop              # live monitor (Ctrl-C to quit)
soltop -i 0.5       # sample every 0.5s
soltop --once       # print one frame and exit
soltop --version
```

## Requirements

- Apple Silicon Mac (M1 or newer)
- macOS with the system `python3`

## Notes / accuracy

- No `sudo` and no `powermetrics` dependency.
- GPU utilization and power come from IOReport residency / energy counters;
  they track trends well but are approximations, not firmware-exact values.
- CPU cluster frequency is approximate (derived from DVFS residency; the CPU
  voltage-states raw unit is normalized to a known max). GPU frequency is exact.

## License

MIT — see [LICENSE](LICENSE).
