# Hardware ground truth

Raw captures from real machines. Every frequency figure soltop prints was
calibrated against these, so keep them when changing `load_dvfs`,
`match_cpu_ladder`, `match_gpu_ladder` or `group_clusters`.

They exist because the naming and numbering in Apple's IORegistry is **not
stable across chips**, and guessing at it produced confidently wrong clocks
more than once. If soltop ever needs to support a new SoC, capture the same
three things on it rather than inferring them:

    python3 tools/dump_dvfs.py     > tools/fixtures/<chip>-voltage-states.txt
    python3 tools/dump_clusters.py > tools/fixtures/<chip>-clusters.txt
    sudo powermetrics --samplers cpu_power -i 1000 -n 2 > <chip>-powermetrics.txt

## M5 Pro (Mac17,9, 15-core: 5 Super + 10 Performance)

| file | what it pins down |
|---|---|
| `m5pro-powermetrics.txt` | the true ladders: S 1308..4608, P0/P1 1344..4380 MHz |
| `m5pro-voltage-states.txt` | which IORegistry keys hold them (5, 22, 23 — *not* the M4's 1 and 5) |
| `m5pro-ioreport-channels.txt` | the core names: `PCPU0..4` are **Super**, `MCPU00..14` are **Performance** |
| `m5pro-energy-model.txt` | the power channels — and why they are read the way they are |

### Why power still reads channels by name

The obvious hardening — discover the per-cluster energy accumulators and sum
them, rather than trusting a name — was **tried and rejected**, because this
capture shows it does not survive the chip change:

    M4 Pro   EACC_CPU, PACC0_CPU, PACC1_CPU        (sum == 'CPU Energy', ratio 1.000)
    M5 Pro   MCPU0, MCPU0_0..4, PCPU, PACC_0/1/2   (nothing matches the M4 pattern)

The *aggregate* names, by contrast, are identical on both chips and report
correct values (`CPU Energy`, `GPU`, `ANE`, `DRAM`). The high-level abstraction
turned out to be the stable one; the detail beneath it is what churns. So
`ENERGY_KEYS` stays — but see `POWER_SANE_MAX_MW`, which guards the failure that
*would* be silent: `GPU Energy` counts in µJ, and reading it as mJ yields 272 kW
on an M4 and 2833 W on an M5. We don't use that channel, but a chip that drops
`GPU` and keeps only `GPU Energy` would otherwise render a four-digit wattage
with total confidence.

The three surprises this chip sprang, all reproduced in `test_soltop.py`:

1. **No E-cluster.** Apple dropped efficiency cores from the M5 Pro. It is 5
   Super (4.6 GHz) + 10 Performance (4.4 GHz) cores, which the voltage-states
   tables confirm to the megahertz (4608 / 4380).
2. **`PCPU*` means the opposite of what it means on an M4.** Here it is the
   *Super* cluster; the performance cores are `MCPU*`. So a core's letter can
   never name its tier — soltop ranks tiers by ladder ceiling instead.
3. **The GPU's state count is not its ladder length.** It reports a fixed
   `P1..P15` on both chips while the real ladder has 15 entries on an M4 and 13
   on an M5, so matching a GPU to a table by length bound it to a *CPU* ladder.

Note `P1-Cluster` shows `down residency: 100.00%` in the powermetrics capture:
macOS had that whole cluster powered off. A cluster reading 0% with no clock is
correct behaviour, not a bug.
