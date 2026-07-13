"""soltop -- an Apple Silicon GPU/CPU/power monitor that needs no sudo.

    ffi         raw ctypes bindings (CoreFoundation, IOReport, IOKit, libc)
    core/       reading the hardware -- see core/__init__.py
    exporter/   machine-readable output (JSON, CSV, Prometheus)
    ui          terminal rendering and the live loop
    cli         argument parsing and entry point

`import soltop` re-exports the whole public surface, so embedders and the tests
need not know which module a name lives in.
"""

__version__ = "0.11.1"

from .ffi import CF, IOR, IOKIT, cfstr, from_cfstr

from .core.dvfs import (
    CPU_PERIOD_NUMERATOR, DVFS, IDLE_NAMES, _GPU_MAX_MHZ, _SANE_MHZ,
    _VOLTAGE_STATE_RE, _decode_ladder, _is_sram, _nsteps, _one_ladder,
    _pstate_index, _rank_key, _read_voltage_state_tables, active_ratio,
    cluster_freq_mhz, is_idle_state, load_dvfs, match_cpu_ladder,
    match_gpu_ladder,
)
from .core.power import ENERGY_KEYS, POWER_LABELS, POWER_SANE_MAX_MW
from .core.sampler import (
    FIRST_SAMPLE_MAX_INTERVAL, Sampler, _NOT_UTIL, _UTIL_SUBGROUPS,
    _fallback_state_subgroups,
    build_subscription, classify_group, copy_group, discover_state_channels,
    iter_channels, read_states,
)
from .core.cpu import (
    _CORE_RE, _EFFICIENCY_MAX_RATIO, _core_kind_and_digits, _tier_labels,
    cpu_view, group_clusters,
)
from .core.gpu import gpu_view
from .core.process import (
    GPU_CLIENT_CLASSES, ProcGPUSampler, _attach_proc_stats, _gpu_client_totals,
)
from .core.system import (
    THERMAL_NAMES, machine_name, mem_stats, thermal_state,
)
from .core.temps import die_temps, soc_temp
from .core.view import organize

from .exporter.formats import (
    _csv_columns, snapshot, to_csv_row, to_json, to_prometheus,
)
from .exporter.serve import _parse_addr, _sample_snapshots, serve, stream

from .ui import (
    GRAPH_HEIGHT, HEADER, KeyReader, LIVE_MAX_RETRIES, RESET, _ANSI_RE,
    _fmt_bytes, _freq_txt, _truncate_visible, _visible_len, bar_width_for,
    bracket_gauge, color_for, gauge_bar, hgauge, live, render, render_cores,
    render_procs, render_soc, term_size, vgraph, wrap_box, ANE_SCALE_W,
)
from .cli import main
