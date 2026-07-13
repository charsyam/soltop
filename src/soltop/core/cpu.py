"""CPU: group cores into clusters and name the E/P/S tiers.

Both the layout and the tier names are DERIVED, never assumed: a core's letter
does not mean the same thing across chips ('PCPU' is Performance on an M4 and
Super on an M5), so clusters split on the name's digit structure and tiers rank
by their measured ladder ceiling.
"""
import re

from . import dvfs as _dvfs
from .dvfs import _nsteps, cluster_freq_mhz, match_cpu_ladder


_CORE_RE = re.compile(r"^([A-Z]+)CPU(\d+)$")   # ECPU000 -> ('E','000'); MCPU14 -> ('M','14')

# A slower tier is an EFFICIENCY cluster only if it is much slower than the
# fastest one. An M4 Pro's E-cluster tops out at 2592 MHz against the P's 4512
# (57%), while an M5 Pro's two tiers are 4380 and 4608 (95%) -- both are
# performance-class, the faster being Apple's "Super" cores. This ratio is what
# tells "E below P" apart from "P below S"; the core-name letters cannot, since
# 'PCPU' means Performance on an M4 and Super on an M5.


# A slower tier is an EFFICIENCY cluster only if it is much slower than the
# fastest one. An M4 Pro's E-cluster tops out at 2592 MHz against the P's 4512
# (57%), while an M5 Pro's two tiers are 4380 and 4608 (95%) -- both are
# performance-class, the faster being Apple's "Super" cores. This ratio is what
# tells "E below P" apart from "P below S"; the core-name letters cannot, since
# 'PCPU' means Performance on an M4 and Super on an M5.
_EFFICIENCY_MAX_RATIO = 0.75


def _tier_labels(tiers):
    """Name the CPU tiers from their ladder ceilings: {kind: 'E'|'P'|'S'}.

    ``tiers`` maps a core-name kind ('E', 'P', 'M', ...) to that tier's top
    ladder MHz. Ranking by ceiling rather than by letter is what makes the
    labels agree with powermetrics on both chips:

        M4 Pro   E 2592 / P 4512  -> the slow tier is 57% of the fast: E, P
        M5 Pro   M 4380 / P 4608  -> the slow tier is 95% of the fast: P, S
                                     (so M5's 'PCPU*' correctly reads as S)

    A tier whose ladder is unknown (0) keeps its raw letter rather than being
    guessed at. A single-tier chip is just 'P'.
    """
    known = {k: v for k, v in tiers.items() if v}
    if not known:
        return {k: k for k in tiers}
    top = max(known.values())
    labels = {}
    for kind, ceiling in tiers.items():
        if not ceiling:
            labels[kind] = kind          # unknown ladder: do not invent a tier
        elif ceiling == top:
            # The fastest tier is 'S' only when a *performance*-class tier sits
            # below it; if the tier below is an efficiency one, this is just P.
            others = [v for k, v in known.items() if k != kind]
            has_perf_below = any(v >= top * _EFFICIENCY_MAX_RATIO for v in others)
            labels[kind] = "S" if has_perf_below else "P"
        elif ceiling >= top * _EFFICIENCY_MAX_RATIO:
            labels[kind] = "P"
        else:
            labels[kind] = "E"
    return labels


def _core_kind_and_digits(name):
    """('M', '14') from 'MCPU14'; (None, None) if the name is not a core."""
    m = _CORE_RE.match((name or "").upper())
    if not m:
        return (None, None)
    return (m.group(1), m.group(2))


def group_clusters(cores):
    """Group core channels into clusters, deriving the layout from the names.

    IOReport names cores <KIND>CPU<digits>, but the MEANING of those digits is
    chip-specific and cannot be assumed:

        M4 Pro   ECPU000..ECPU030   PCPU000..PCPU140    <- 3 digits: cluster,core,0
        M5 Pro   PCPU0..PCPU4       MCPU00..MCPU14      <- S is 1 digit (core only),
                                                           M is 2 digits (cluster,core)

    So on an M5 'PCPU*' is the S-cluster and 'MCPU*' the performance cores --
    the letter 'P' means the opposite of what it means on an M4. Hardcoding
    either the letters or the digit positions therefore mislabels the clusters
    on the other chip, which is exactly how a P-cluster came to render as
    "CPU other".

    The layout is instead inferred from the DIGIT COUNT, which is what actually
    distinguishes the two cases. A name carries a cluster index only if it has
    room for one *after* the core number:

        PCPU0 .. PCPU4    1 digit  -> core only  => ONE cluster of 5 cores
        MCPU00 .. MCPU14  2 digits -> cluster,core => TWO clusters of 5
        PCPU000 .. PCPU140  3 digits -> cluster,core,0 => TWO clusters of 5

    Splitting on the leading digit regardless would turn the M5's five S-cores
    (PCPU0..PCPU4, five distinct leading digits) into five one-core clusters.

    The E/P/S letter shown to the user is likewise not taken from the name -- it
    is derived from each tier's ladder ceiling by _tier_labels, so that an M5's
    'PCPU*' correctly reads as S (Super) rather than P.
    """
    tables = _dvfs.tables()
    by_kind = {}
    order = []
    other = []
    for e in cores:
        kind, digits = _core_kind_and_digits(e.get("name"))
        if kind is None:
            other.append(e)
            continue
        if kind not in by_kind:
            by_kind[kind] = []
            order.append(kind)
        by_kind[kind].append((digits, e))

    # Each kind's ceiling = the top of the ladder its cores' P-state count binds.
    ceilings = {}
    for kind, members in by_kind.items():
        ladder = match_cpu_ladder(_nsteps([e for _, e in members]), tables)
        ceilings[kind] = ladder[-1] if ladder else 0.0
    letters = _tier_labels(ceilings)

    clusters = {}
    corder = []

    def add(key, label, entry):
        if key not in clusters:
            clusters[key] = {"key": key, "label": label, "cores": []}
            corder.append(key)
        clusters[key]["cores"].append(entry)

    for kind in order:
        members = by_kind[kind]
        letter = letters.get(kind, kind)
        # A leading cluster digit exists only when the name has >= 2 digits (one
        # for the cluster, at least one for the core). Single-digit names are
        # core indices within one cluster.
        widths = {len(d) for d, _ in members}
        indexed = len(widths) == 1 and widths.pop() >= 2
        leading = {d[0] for d, _ in members}
        if indexed and len(leading) > 1:
            for digits, e in members:
                idx = digits[0]
                add(f"{letter}{idx}", f"CPU {letter}{idx}-cluster", e)
        else:
            for _, e in members:
                add(letter, f"CPU {letter}-cluster", e)

    for e in other:
        add("?", "CPU other", e)

    return [clusters[k] for k in corder]


def cpu_view(raw, tables=None):
    """[{"key","label","avg","count","mhz","per_core","cores"}] per cluster.

    Each cluster binds its OWN ladder, by its own P-state count -- the
    voltage-states numbering differs per chip (see match_cpu_ladder).
    """
    tables = _dvfs.tables() if tables is None else (tables or {})
    # The per-core state-residency channels only; the COMPLEX aggregates are a
    # different thing and would double-count.
    cores = [e for e in raw.get("cpu", [])
             if "COMPLEX" not in (e["subgroup"] or "").upper()]

    out = []
    for c in group_clusters(cores):
        n = len(c["cores"])
        table = match_cpu_ladder(_nsteps(c["cores"]), tables)
        avg = sum(x["active"] for x in c["cores"]) / n * 100
        # Per-core figures for the 'c' view, computed here so the DVFS tables
        # stay out of the display layer.
        per_core = [{"name": x["name"], "pct": x["active"] * 100,
                     "mhz": cluster_freq_mhz([x], table)} for x in c["cores"]]
        out.append({**c, "avg": avg, "count": n,
                    "mhz": cluster_freq_mhz(c["cores"], table),
                    "per_core": per_core})
    return out
