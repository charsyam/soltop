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
