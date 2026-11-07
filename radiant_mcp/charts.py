"""Chart rendering for the D.A.V.I.D chart tools (RAD-254).

Pure functions: data in, PNG bytes (plus the statistics drawn) out. The tools
in ``tools/charts.py`` fetch the data and call these. matplotlib runs headless
(``Agg``); Kaplan-Meier estimates come from SciPy (``scipy.stats.ecdf`` on
right-censored data); the log-rank test is the k-sample form (chi-square,
k - 1 degrees of freedom), equal to ``scipy.stats.logrank`` for two groups.
"""

import functools
import io
import math
import os
import threading
from dataclasses import dataclass, field
from typing import Optional, Sequence

# matplotlib reads MPLCONFIGDIR on import; the container user has no home directory.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Circle, Rectangle  # noqa: E402

MAX_PNG_BYTES = 1_000_000
_DPI = 110
_PALETTE = ("#1C75BC", "#E4572E", "#2A9D8F", "#F4A261", "#6A4C93", "#8D99AE")
MAX_SURVIVAL_GROUPS = len(_PALETTE)
_GRID = {"alpha": 0.3}

# pyplot keeps global state and the tools run on worker threads: draw one chart at a time.
_RENDER_LOCK = threading.Lock()


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _RENDER_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def to_png(fig) -> bytes:
    """Encode and close ``fig``; lower the resolution until it fits MAX_PNG_BYTES."""
    try:
        for dpi in (_DPI, 90, 72, 56):
            buffer = io.BytesIO()
            fig.savefig(buffer, format="png", dpi=dpi, bbox_inches="tight")
            data = buffer.getvalue()
            if len(data) <= MAX_PNG_BYTES:
                return data
        raise ValueError("chart too large to encode under 1 MB")
    finally:
        plt.close(fig)


# -- Venn ------------------------------------------------------------------------

# Region keys are tuples of membership flags, in the order of the labels.
_VENN3 = {
    "circles": ((0.0, 0.32), (-0.32, -0.2), (0.32, -0.2)),
    "radius": 0.55,
    "regions": {
        (1, 0, 0): (0.0, 0.62), (0, 1, 0): (-0.6, -0.38), (0, 0, 1): (0.6, -0.38),
        (1, 1, 0): (-0.32, 0.18), (1, 0, 1): (0.32, 0.18), (0, 1, 1): (0.0, -0.45),
        (1, 1, 1): (0.0, 0.0),
    },
    "labels": ((0.0, 0.98), (-0.95, -0.85), (0.95, -0.85)),
}
_VENN2 = {
    "circles": ((-0.3, 0.0), (0.3, 0.0)),
    "radius": 0.55,
    "regions": {(1, 0): (-0.55, 0.0), (0, 1): (0.55, 0.0), (1, 1): (0.0, 0.0)},
    "labels": ((-0.55, 0.68), (0.55, 0.68)),
}


@_serialized
def venn(labels: Sequence[str], counts: dict[tuple[int, ...], int], title: str) -> bytes:
    """Non-scaled 2- or 3-set Venn diagram; ``counts`` maps membership flags to counts."""
    layout = {2: _VENN2, 3: _VENN3}.get(len(labels))
    if layout is None:
        raise ValueError("a Venn diagram needs 2 or 3 sets")
    fig, ax = plt.subplots(figsize=(6.5, 5.6))
    for (x, y), color in zip(layout["circles"], _PALETTE, strict=False):
        ax.add_patch(Circle((x, y), layout["radius"], facecolor=color, alpha=0.25, edgecolor=color, linewidth=2))
    for region, (x, y) in layout["regions"].items():
        ax.text(x, y, f"{counts.get(region, 0):,}", ha="center", va="center", fontsize=13, fontweight="bold")
    for label, (x, y) in zip(labels, layout["labels"], strict=True):
        ax.text(x, y, label, ha="center", va="center", fontsize=12)
    ax.set(xlim=(-1.3, 1.3), ylim=(-1.05, 1.1), aspect="equal", title=title)
    ax.axis("off")
    return to_png(fig)


# -- survival ----------------------------------------------------------------------

@dataclass
class SurvivalGroup:
    name: str
    times: list[float]  # years
    events: list[bool]  # True = death observed, False = censored


@dataclass
class SurvivalStats:
    groups: list[dict] = field(default_factory=list)
    logrank_p: Optional[float] = None
    logrank_df: Optional[int] = None


def _censored(group: SurvivalGroup):
    from scipy.stats import CensoredData

    times = np.asarray(group.times, dtype=float)
    events = np.asarray(group.events, dtype=bool)
    return CensoredData(uncensored=times[events], right=times[~events])


def _km_steps(group: SurvivalGroup) -> tuple[np.ndarray, np.ndarray]:
    """Kaplan-Meier survival function as step points, from 0."""
    from scipy.stats import ecdf

    if not any(group.events):
        end = max(group.times) if group.times else 0.0
        return np.array([0.0, end]), np.array([1.0, 1.0])
    sf = ecdf(_censored(group)).sf
    xs = np.concatenate(([0.0], sf.quantiles))
    ys = np.concatenate(([1.0], sf.probabilities))
    end = max(group.times)
    if end > xs[-1]:
        xs, ys = np.append(xs, end), np.append(ys, ys[-1])
    return xs, ys


def logrank(groups: Sequence[SurvivalGroup]) -> Optional[tuple[float, int, float]]:
    """k-sample log-rank test: ``(chi-square, degrees of freedom, p-value)``,
    or None without any death or with fewer than two groups."""
    from scipy.stats import chi2

    k = len(groups)
    times = np.concatenate([np.asarray(g.times, dtype=float) for g in groups]) if groups else np.array([])
    events = np.concatenate([np.asarray(g.events, dtype=bool) for g in groups]) if groups else np.array([], bool)
    if k < 2 or not events.any():
        return None
    member = np.concatenate([np.full(len(g.times), i) for i, g in enumerate(groups)])
    observed_minus_expected = np.zeros(k)
    variance = np.zeros((k, k))
    for t in np.unique(times[events]):
        at_risk = np.bincount(member[times >= t], minlength=k).astype(float)
        deaths = np.bincount(member[(times == t) & events], minlength=k).astype(float)
        n, d = at_risk.sum(), deaths.sum()
        observed_minus_expected += deaths - d * at_risk / n
        if n > 1:
            share = at_risk / n
            variance += d * (n - d) / (n - 1) * (np.diag(share) - np.outer(share, share))
    try:
        statistic = float(observed_minus_expected[:-1] @ np.linalg.solve(variance[:-1, :-1], observed_minus_expected[:-1]))
    except np.linalg.LinAlgError:
        return None
    return statistic, k - 1, float(chi2.sf(statistic, k - 1))


def _median(xs: np.ndarray, ys: np.ndarray) -> Optional[float]:
    below = np.nonzero(ys <= 0.5)[0]
    return float(xs[below[0]]) if below.size else None


@_serialized
def survival(groups: Sequence[SurvivalGroup], title: str, test: bool = True) -> tuple[bytes, SurvivalStats]:
    """Kaplan-Meier curves with censoring marks, numbers at risk and, when
    ``test``, the log-rank p-value across the groups."""
    groups = [g for g in groups if g.times]
    if not groups:
        raise ValueError("no patient with survival data")
    stats = SurvivalStats()
    horizon = max(max(g.times) for g in groups)
    ticks = np.linspace(0, math.ceil(horizon), num=min(6, math.ceil(horizon) + 1)) if horizon > 0 else np.array([0.0])
    fig, (ax, table) = plt.subplots(2, 1, figsize=(7.5, 5.2 + 0.2 * len(groups)), height_ratios=(4, 0.4 + 0.3 * len(groups)),
                                    sharex=True)
    for group, color in zip(groups, _PALETTE, strict=False):
        xs, ys = _km_steps(group)
        ax.step(xs, ys, where="post", color=color, label=f"{group.name} (n={len(group.times)})")
        censored = [t for t, e in zip(group.times, group.events, strict=True) if not e]
        if censored:
            heights = np.interp(censored, xs, ys, left=1.0, right=ys[-1])
            ax.plot(censored, heights, "|", color=color, markersize=8)
        median = _median(xs, ys)
        stats.groups.append({
            "group": group.name, "patients": len(group.times), "deaths": int(sum(group.events)),
            "median_survival_years": round(median, 2) if median is not None else None,
        })
    result = logrank(groups) if test else None
    if result is not None:
        _, stats.logrank_df, stats.logrank_p = result
        ax.text(0.98, 0.04, f"log-rank p = {stats.logrank_p:.3g}", transform=ax.transAxes, ha="right")
    ax.set(ylabel="Survival probability", ylim=(0, 1.05), title=title)
    ax.grid(**_GRID)
    ax.legend(loc="lower left")
    for row, (group, color) in enumerate(zip(groups, _PALETTE, strict=False)):
        times = np.asarray(group.times)
        for tick in ticks:
            table.text(tick, row, str(int((times >= tick).sum())), ha="center", va="center", color=color)
    table.set(ylim=(len(groups) - 0.5, -0.5), xlabel="Years since initial diagnosis")
    table.set_yticks(range(len(groups)), [g.name for g in groups])
    table.set_xticks(ticks)
    table.set_title("Number at risk", fontsize=9, loc="left")
    for spine in table.spines.values():
        spine.set_visible(False)
    return to_png(fig), stats


# -- oncoprint ---------------------------------------------------------------------

ALTERATION_COLORS = {
    "truncating": "#D62828", "splice": "#F77F00", "missense": "#2A9D8F", "inframe": "#8AB17D",
    "other": "#ADB5BD", "LOSS": "#1D3557", "GAIN": "#E76F51", "CNLOH": "#6A4C93", "GAINLOH": "#B5838D",
    "multiple": "#000000",
}
_EMPTY = "#EEF1F4"


@_serialized
def oncoprint(genes: Sequence[str], cases: Sequence[str], cells: dict[tuple[str, str], str], title: str) -> bytes:
    """Gene x case matrix; ``cells[(gene, case)]`` is a key of ALTERATION_COLORS."""
    width = min(18.0, max(5.0, 0.18 * len(cases) + 2.5))
    fig, ax = plt.subplots(figsize=(width, 0.42 * len(genes) + 1.4))
    for row, gene in enumerate(genes):
        for col, case in enumerate(cases):
            kind = cells.get((gene, case))
            ax.add_patch(Rectangle((col, row), 0.85, 0.8, color=ALTERATION_COLORS.get(kind, _EMPTY)))
    ax.set(xlim=(-0.2, len(cases)), ylim=(len(genes), -0.2), title=title)
    ax.set_yticks([r + 0.4 for r in range(len(genes))], genes)
    if len(cases) <= 40:
        ax.set_xticks([c + 0.42 for c in range(len(cases))], cases, rotation=90, fontsize=7)
    else:
        ax.set_xticks([])
        ax.set_xlabel(f"{len(cases)} cases")
    for spine in ax.spines.values():
        spine.set_visible(False)
    used = [k for k in ALTERATION_COLORS if k in set(cells.values())]
    ax.legend(handles=[Rectangle((0, 0), 1, 1, color=ALTERATION_COLORS[k], label=k) for k in used],
              loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False)
    return to_png(fig)


# -- lollipop ----------------------------------------------------------------------

@dataclass
class Lollipop:
    position: int
    label: str
    carriers: int
    kind: str  # key of ALTERATION_COLORS


@_serialized
def lollipop(points: Sequence[Lollipop], title: str, subtitle: str) -> bytes:
    """Protein positions of variants, stem height = number of carriers."""
    fig, ax = plt.subplots(figsize=(9, 4))
    length = max(p.position for p in points)
    ax.add_patch(Rectangle((0, -0.25), length * 1.03, 0.25, color="#CED4DA"))
    for point in points:
        color = ALTERATION_COLORS.get(point.kind, ALTERATION_COLORS["other"])
        ax.plot([point.position, point.position], [0, point.carriers], color="#6C757D", linewidth=1)
        ax.plot(point.position, point.carriers, "o", color=color, markersize=8)
    for point in sorted(points, key=lambda p: -p.carriers)[:8]:
        ax.annotate(point.label, (point.position, point.carriers), textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=8)
    ax.set(xlim=(0, length * 1.05), ylim=(-0.4, max(p.carriers for p in points) * 1.3 + 0.5),
           xlabel="Protein position (aa)", ylabel="Carriers")
    ax.set_title(f"{title}\n{subtitle}", fontsize=11)
    ax.grid(axis="y", **_GRID)
    used = sorted({p.kind for p in points}, key=list(ALTERATION_COLORS).index)
    ax.legend(handles=[plt.Line2D([], [], marker="o", linestyle="", color=ALTERATION_COLORS[k], label=k) for k in used],
              loc="upper right", frameon=False)
    return to_png(fig)


# -- timeline ----------------------------------------------------------------------

@dataclass
class TimelineItem:
    track: str
    start: float  # years
    end: Optional[float] = None  # years; None = point event
    label: str = ""


@_serialized
def timeline(items: Sequence[TimelineItem], tracks: Sequence[str], title: str) -> bytes:
    """Patient history: one row per track, point events and periods on an age axis."""
    fig, ax = plt.subplots(figsize=(9, 0.6 * len(tracks) + 1.6))
    rows = {track: i for i, track in enumerate(tracks)}
    for item in items:
        row = rows[item.track]
        color = _PALETTE[row % len(_PALETTE)]
        if item.end is not None and item.end > item.start:
            ax.add_patch(Rectangle((item.start, row - 0.25), item.end - item.start, 0.5, color=color, alpha=0.6))
        else:
            ax.plot(item.start, row, "D", color=color, markersize=7)
        if item.label:
            ax.annotate(item.label, (item.start, row), textcoords="offset points", xytext=(0, 9), fontsize=7)
    starts = [i.start for i in items] + [i.end for i in items if i.end is not None]
    low, high = (min(starts), max(starts)) if starts else (0.0, 1.0)
    pad = max(0.5, (high - low) * 0.05)
    ax.set(xlim=(low - pad, high + pad), ylim=(len(tracks) - 0.4, -0.6), xlabel="Age (years)", title=title)
    ax.set_yticks(range(len(tracks)), tracks)
    ax.grid(axis="x", **_GRID)
    return to_png(fig)


# -- bars --------------------------------------------------------------------------

@_serialized
def bar_chart(categories: Sequence[str], values: Sequence[float], title: str,
              x_label: str = "", y_label: str = "", horizontal: bool = False) -> bytes:
    fig, ax = plt.subplots(figsize=(8, max(3.5, 0.3 * len(categories) + 1) if horizontal else 4.5))
    if horizontal:
        ax.barh(categories, values, color=_PALETTE[0])
        ax.invert_yaxis()
        ax.set(xlabel=y_label, ylabel=x_label)
        ax.grid(axis="x", **_GRID)
    else:
        ax.bar(categories, values, color=_PALETTE[0])
        ax.set(xlabel=x_label, ylabel=y_label)
        ax.grid(axis="y", **_GRID)
        if max((len(c) for c in categories), default=0) > 6 or len(categories) > 8:
            plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    ax.set_title(title)
    return to_png(fig)
