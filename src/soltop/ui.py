"""Terminal rendering: gauges, graphs, the boxed dashboard, and the live loop."""
import os
import re
import select
import sys
import termios
import time
import tty
from collections import deque

from . import __version__
from .core.power import POWER_LABELS
from .core.process import ProcGPUSampler
from .core.sampler import Sampler
from .core.system import THERMAL_NAMES, machine_name, mem_stats, thermal_state
from .core.view import organize


def _fmt_bytes(n):
    """'1.4G' / '331M' -- a memory figure that fits the narrow MEM column.

    Rounds before choosing the unit, so 1023.7 MiB reads as '1.0G' rather than
    the '1024M' a naive threshold produces.
    """
    if not n:
        return "-"
    for unit, scale in (("T", 1 << 40), ("G", 1 << 30)):
        if round(n / scale, 1) >= 1.0:
            return f"{n / scale:.1f}{unit}"
    return f"{n / (1 << 20):.0f}M"


def render_procs(rows, limit=10):
    """Render a per-process GPU table (like nvidia-smi's process list).

    MEM is the process's RSS: Apple Silicon memory is unified, so that is the
    memory it costs the SoC -- the GPU driver publishes no separate VRAM figure.
    """
    lines = [f"{HEADER} GPU processes{RESET}"]
    if not rows:
        lines.append("  \x1b[2m(no GPU activity)\x1b[0m")
        return lines
    lines.append(f"  {'PID':>7}  {'GPU ms/s':>9}  {'GPU%':>5}  {'CPU%':>6}"
                 f"  {'MEM':>6}  NAME")
    for r in rows[:limit]:
        pct = min(100.0, r["gpu_ms_s"] / 10.0)   # 1000 ms/s == 100%
        cpu = r.get("cpu_pct")
        cpu_s = f"{cpu:.1f}" if cpu is not None else "-"
        lines.append(f"  {r['pid']:>7}  {r['gpu_ms_s']:>9.1f}  {pct:>5.1f}"
                     f"  {cpu_s:>6}  {_fmt_bytes(r.get('rss_bytes')):>6}"
                     f"  {r['name']}")
    return lines

ESC = "\x1b["
HIDE_CURSOR = ESC + "?25l"
SHOW_CURSOR = ESC + "?25h"
CLEAR = ESC + "2J" + ESC + "H"
HOME = ESC + "H"
CLEAR_TO_END = ESC + "0J"   # clear from cursor to end of screen (removes leftovers)

HEADER = "\x1b[1;92m"       # bold green for section titles
RESET = "\x1b[0m"

# Rows in each history graph. Keep this EVEN: a row's value is (level+1)/height,
# so a true 50% row -- the one vgraph() labels as the half-scale mark -- exists
# only at even heights. At the previous height of 5 the rows sat at
# 20/40/60/80/100 and there was no 50% row to label at all.
GRAPH_HEIGHT = 6

# 1/8-step blocks that fill from bottom to top
EIGHTHS = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


def color_for(pct):
    # Bold ("1;") as well as bright ("9x"): on most terminal themes the bright
    # colours alone render fairly washed out, and these bars are the thing the
    # eye should land on first.
    if pct >= 80:
        return "\x1b[1;91m"  # red
    if pct >= 50:
        return "\x1b[1;93m"  # yellow
    return "\x1b[1;92m"      # green


def vgraph(history, height=8, width=48, label_max=None, label_unit="%",
           color=None):
    """Draw history(%) as a vertical bar graph that grows from bottom to top.

    Oldest value on the left, newest on the right; column height is utilization.
    The top and middle rows carry a y-axis label. If label_max is given, those
    labels show that scale (e.g. watts) instead of %, with the top == label_max.
    """
    vals = list(history)[-width:]
    vals = [0.0] * (width - len(vals)) + vals  # pad the left with empty values

    # Convert each column to a count of 1/8 ticks
    ticks = [int(round(max(0.0, min(100.0, v)) / 100 * height * 8)) for v in vals]

    rows = []
    for r in range(height):                     # r=0 is the top row
        level = height - 1 - r                   # bottom-referenced cell for this row
        cells = []
        for i, t in enumerate(ticks):
            fill = t - level * 8
            fill = 0 if fill < 0 else (8 if fill > 8 else fill)
            ch = EIGHTHS[fill]
            c = (color or color_for(vals[i])) if fill > 0 else ""
            cells.append(f"{c}{ch}\x1b[0m" if fill > 0 else ch)
        # Label the full-scale row and the half-scale row. Each row spans a band,
        # and the row's top edge is (level+1)/height -- so a true 50% row exists
        # only when height is even (at the old height=5 the rows sat at
        # 20/40/60/80/100 and there was simply no 50% row to label).
        axis = (level + 1) / height * 100
        if r == 0 or abs(axis - 50) < 1e-9:
            if label_max is not None:
                lab = f"{axis / 100 * label_max:.0f}{label_unit}"
            else:
                lab = f"{axis:.0f}{label_unit}"
        else:
            lab = ""
        rows.append(f"  {lab:>4}│{''.join(cells)}")
    rows.append("      └" + "─" * width)
    return rows


def term_size():
    import shutil
    s = shutil.get_terminal_size(fallback=(80, 24))
    return s.columns, s.lines


def bar_width_for(cols):
    """Inner bar width that fits the terminal (leaves room for the box border)."""
    return max(10, min(140, cols - 10))


TRACK = "\x1b[90m"          # dim gray for the unfilled gauge track


def gauge_bar(frac, width):
    """Solid '█' fill over a dim '░' track.

    The fill used to be '▏' (a left-eighth block), which paints only 1/8 of each
    cell -- so the bar read as washed out no matter which colour it was given.
    """
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    c = color_for(frac * 100)
    return f"{c}{'█' * filled}{TRACK}{'░' * (width - filled)}{RESET}"


def bracket_gauge(frac, width):
    """The bracketed gauge, '[████░░░░]'. The one place the chrome is defined.

    Both the dashboard (via hgauge) and the per-core view render through this, so
    the two cannot drift apart -- which is exactly how the per-core bars ended up
    using a different glyph than the cluster bars.
    """
    return f"[{gauge_bar(frac, width)}]"


def hgauge(label, frac, width, value=""):
    """asitop-style gauge: a title line, then a filled gauge bar.

    Returns a list of two strings.
    """
    title = f"  {label}" + (f"   {value}" if value else "")
    return [title, f"  {bracket_gauge(frac, width)}"]


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _visible_len(s):
    return len(_ANSI_RE.sub("", s))


def _truncate_visible(s, width):
    """Cut s to `width` visible columns, keeping ANSI escapes intact.

    Escape sequences cost no width, so they are copied through; a trailing RESET
    is appended if anything was dropped, otherwise the color would bleed on.
    """
    if _visible_len(s) <= width:
        return s
    out, shown, pos = [], 0, 0
    for m in _ANSI_RE.finditer(s):
        for ch in s[pos:m.start()]:
            if shown >= width:
                return "".join(out) + RESET
            out.append(ch)
            shown += 1
        out.append(m.group())
        pos = m.end()
    for ch in s[pos:]:
        if shown >= width:
            break
        out.append(ch)
        shown += 1
    return "".join(out) + RESET


def wrap_box(lines, cols, title=""):
    """Wrap content lines in a full box border, title embedded in the top edge.

    Content wider than the box is truncated so the right border never shifts.
    """
    inner = max(4, cols - 2)
    t = f" {title} " if title else ""
    tv = _visible_len(t)
    if tv > inner:
        t = _truncate_visible(t, inner)
        tv = _visible_len(t)
    out = ["┌" + t + "─" * (inner - tv) + "┐"]
    for ln in lines:
        ln = _truncate_visible(ln, inner)
        pad = inner - _visible_len(ln)
        out.append("│" + ln + " " * max(0, pad) + "│")
    out.append("└" + "─" * inner + "┘")
    return out


def _freq_txt(mhz):
    """'@ 1398 MHz', or nothing when the clock is unknown."""
    if not mhz:
        return ""
    return f"@ {mhz:.0f} MHz"


def render_cores(view, width, limit=None):
    """Render every CPU core individually, grouped by cluster.

    The dashboard shows one gauge per cluster (the E/P averages); this is the
    same data broken out per core, so a single pegged core is visible instead of
    being averaged away.
    """
    # Same gauge as the dashboard's cluster bars (gauge_bar, in brackets), so the
    # two views read as one UI. Leave room for the name, the "100.00%" column and
    # the "@ 100% DVFS" suffix, or the bar pushes them past the border and
    # wrap_box() truncates them away.
    bw = max(8, width - 32)
    lines = []
    for c in view.get("clusters", []):
        if limit is not None and len(lines) >= limit:
            break
        ftxt = _freq_txt(c.get("mhz", 0.0))
        head = f"{HEADER} {c['label']}  ({c['count']} cores)  avg {c['avg']:.1f}%"
        lines.append(head + (f"  {ftxt}" if ftxt else "") + RESET)
        for core in c.get("per_core", []):
            if limit is not None and len(lines) >= limit:
                break
            cf = _freq_txt(core.get("mhz", 0.0))
            pct = core["pct"]
            lines.append("  %-8s %s %6.2f%%  %s"
                         % (core["name"], bracket_gauge(pct / 100, bw), pct, cf))
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        lines.append("  \x1b[2m(no CPU cores found)\x1b[0m")
    return lines


def render(view, cols=80, gpu_hist=None, procs=None, height=None, soc_hist=None,
           process_only=False, single_sample=False, core_only=False):
    """Draw the organize() result (view). Does no data-structure reasoning.

    ``single_sample`` suppresses the avg/peak columns, which carry no information
    when only one sample has been taken (they would all just equal the current).
    """
    width = bar_width_for(cols)
    lines = []

    # Box title: app name + machine name + thermal/throttle state.
    title = f"Soltop v{__version__} · {machine_name()}"
    ts = thermal_state()
    if ts >= 0:
        tcolor = {0: "\x1b[92m", 1: "\x1b[93m", 2: "\x1b[91m", 3: "\x1b[91m"}.get(ts, "")
        tname = THERMAL_NAMES.get(ts, "?")
        thr = " throttling" if ts >= 1 else ""
        title += f"   thermal: {tcolor}{tname}{thr}{RESET}"

    def _fit(lines, title):
        """Pad/clip content to the frame and draw the border."""
        if height:
            content_height = max(0, height - 2)
            del lines[content_height:]
            while len(lines) < content_height:
                lines.append("")
        return ("\x1b[K\n").join(wrap_box(lines, cols, title)) + "\x1b[K"

    if process_only:
        title = f"Soltop v{__version__} · GPU Processes · p: dashboard · q: quit"
        limit = max(1, height - 4) if height else 10
        lines.extend(render_procs(procs or [], limit=limit))
        return _fit(lines, title)

    if core_only:
        title = f"Soltop v{__version__} · CPU Cores · c: dashboard · q: quit"
        limit = max(1, height - 2) if height else None
        lines.extend(render_cores(view, width, limit=limit))
        return _fit(lines, title)

    title += "   p: processes   c: cores   q: quit"

    freq_txt = _freq_txt(view.get("gpu_mhz", 0.0))
    cur = view["gpu_pct"]
    if single_sample:
        stats = ""
    elif gpu_hist:
        stats = f"  (avg {sum(gpu_hist) / len(gpu_hist):.1f}%  peak {max(gpu_hist):.1f}%)"
    else:
        stats = ""
    lines.append(f"{HEADER} GPU Usage: {cur:.1f}%{stats}  {freq_txt}{RESET}")
    if gpu_hist is not None:
        lines.extend(vgraph(gpu_hist, height=GRAPH_HEIGHT, width=max(10, width - 7)))
    lines.append("")

    # Power: cur/avg/peak table for components; total as an asitop-style gauge.
    power = view.get("power", {})
    pavg = view.get("power_avg", {})
    ppeak = view.get("power_peak", {})
    if power:
        # Components on one line, each with W unit and cur/avg/peak values.
        def triple(lbl):
            if single_sample:
                return f"{lbl} {power[lbl] / 1000:.1f}W"
            return (f"{lbl} {power[lbl] / 1000:.1f}/"
                    f"{pavg.get(lbl, 0) / 1000:.1f}/{ppeak.get(lbl, 0) / 1000:.1f}W")
        comp = " | ".join(triple(lbl) for lbl in POWER_LABELS if lbl in power)
        soc_w = power.get("SoC", 0) / 1000
        if single_sample:
            pstats = ""
        else:
            pstats = (f"  (avg {pavg.get('SoC', 0) / 1000:.1f}W"
                      f"  peak {ppeak.get('SoC', 0) / 1000:.1f}W)")
        lines.append(f"{HEADER} Total Power: {soc_w:.1f}W{pstats}{RESET}")
        # Power history graph: fixed 110 W full-scale, always green (we don't
        # know the real per-machine limit, so don't imply thresholds by color).
        if soc_hist is not None:
            scale = 110.0
            norm = [min(100.0, (w / scale) * 100) for w in soc_hist]
            lines.extend(vgraph(norm, height=GRAPH_HEIGHT, width=max(10, width - 7),
                                label_max=scale, label_unit="W",
                                color="\x1b[1;92m"))
        lines.append(f"  {comp}" + ("" if single_sample else "   (cur/avg/peak)"))
        lines.append("")

    # Memory: asitop-style gauge + text breakdown (wired / compressed / swap).
    mem = mem_stats()
    if mem.get("total"):
        gb = 1_000_000_000
        total = mem["total"] / gb
        used = mem["used"] / gb
        frac = mem["used"] / mem["total"] if mem["total"] else 0.0
        lines.append(f"{HEADER} Memory{RESET}")
        val = (f"{used:.1f}/{total:.1f} GB ({frac * 100:.0f}%)   "
               f"wired {mem['wired'] / gb:.1f}G | "
               f"compressed {mem['compressed'] / gb:.1f}G | "
               f"swap {mem['swap_used'] / gb:.1f}/{mem['swap_total'] / gb:.1f}G")
        lines.extend(hgauge("RAM", frac, width, val))
        lines.append("")

    # CPU: core counts in the title; one asitop-style gauge per cluster + freq.
    counts = "  ".join(f"{c['key']}:{c['count']}" for c in view["clusters"])
    lines.append(f"{HEADER} CPU  ({counts}){RESET}")
    for c in view["clusters"]:
        ftxt = _freq_txt(c.get("mhz", 0.0))
        val = f"{c['avg']:.1f}%" + (f"  {ftxt}" if ftxt else "")
        lines.extend(hgauge(c["label"], c["avg"] / 100, width, val))
    lines.append("")

    # Per-process GPU table LAST: its row count changes, so keep it at the
    # bottom to avoid shifting everything above it. Limit rows to fit the screen
    # (reserve the top/bottom border + column-title rows).
    if height:
        limit = max(1, height - len(lines) - 6)
    else:
        limit = 10
    lines.extend(render_procs(procs or [], limit=limit))

    if lines and lines[-1] == "":
        lines.pop()

    # _fit() pads/clips to the screen height (on very short terminals the
    # lower-priority content at the bottom is omitted), draws the outer border,
    # and clears each line end so longer leftovers from the prior frame go away.
    return _fit(lines, title)


class KeyReader:
    """Read single terminal keys without blocking, restoring mode on exit."""

    def __init__(self, stream):
        self.stream = stream
        self.fd = None
        self.attrs = None

    def __enter__(self):
        if self.stream.isatty():
            import termios
            import tty
            self.fd = self.stream.fileno()
            self.attrs = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def read_available(self):
        if self.fd is None:
            return ""
        import os
        import select
        keys = []
        while select.select([self.fd], [], [], 0)[0]:
            data = os.read(self.fd, 1)
            if not data:
                break
            keys.append(data.decode("utf-8", "ignore"))
        return "".join(keys)

    def __exit__(self, exc_type, exc_value, traceback):
        if self.fd is not None and self.attrs is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.attrs)


# Consecutive Sampler rebuilds live() will attempt before giving up. A transient
# IOReport failure recovers on the first retry; anything that fails this many
# times running is not transient, and spinning on it forever helps nobody.
LIVE_MAX_RETRIES = 3


def live(interval=1.0, cols_override=None):
    import sys

    sampler = Sampler()
    proc_sampler = ProcGPUSampler()
    gpu_hist = deque(maxlen=200)
    soc_hist = deque(maxlen=200)
    # Clear the whole screen only once; afterwards just move the cursor home
    # and overwrite in place to avoid flicker.
    print(HIDE_CURSOR + CLEAR, end="", flush=True)
    last_size = None
    process_only = False
    core_only = False
    retries = 0
    try:
        with KeyReader(sys.stdin) as keys:
            while True:
                try:
                    view = organize(sampler.read(interval))
                    retries = 0
                except RuntimeError:
                    # read() closes the Sampler and raises when IOReport keeps
                    # failing, so without this a transient hiccup would kill the
                    # monitor with a traceback. Rebuild and carry on -- but only
                    # a bounded number of times in a row: retrying forever would
                    # spin, rebuilding a subscription (an IOReportCopyAllChannels
                    # scan over ~11k channels) several times a second behind a
                    # frozen screen, with the user told nothing.
                    retries += 1
                    if retries > LIVE_MAX_RETRIES:
                        raise
                    sampler.close()
                    sampler = Sampler()
                    time.sleep(interval)     # don't hot-spin while it's broken
                    continue
                gpu_hist.append(view["gpu_pct"])
                soc_hist.append(view.get("power", {}).get("SoC", 0) / 1000)
                try:
                    procs = proc_sampler.step(interval)
                except Exception:
                    procs = []
                # Handle each key in order: 'p' toggles the process view, 'c' the
                # per-core view (each returns to the dashboard when pressed again,
                # and the two views are mutually exclusive), 'q'/ESC quits.
                for k in keys.read_available().lower():
                    if k == "p":
                        process_only = not process_only
                        core_only = False
                    elif k == "c":
                        core_only = not core_only
                        process_only = False
                    elif k in ("q", "\x1b", "\x03"):
                        return
                tcols, rows = term_size()
                cols = cols_override or tcols
                # On resize, wipe the screen once so wrapped/old lines don't linger.
                if (cols, rows) != last_size:
                    print(CLEAR, end="")
                    last_size = (cols, rows)
                frame = render(view, cols, gpu_hist, procs, height=rows,
                               soc_hist=soc_hist, process_only=process_only,
                               core_only=core_only)
                # Overwrite the whole frame in one write, then clear any lines below.
                print(HOME + frame + CLEAR_TO_END, end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sampler.close()
        print(SHOW_CURSOR + "\n", end="", flush=True)
