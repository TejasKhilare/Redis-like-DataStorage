"""Charts and tables from a suite result file.

    python -m benchmarks.report benchmarks/results/run.json

Writes ``docs/benchmarks/<chart>-light.png`` and ``-dark.png`` (the README
and BENCHMARKS.md pick one with ``<picture>``) and ``docs/benchmarks/results.md``,
every number in table form. Needs matplotlib: ``pip install -e ".[bench]"``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Each server keeps its colour on every chart (validated categorical slots 1-4).
ENTITIES = ("kvstore", "redis", "http", "router")
LABELS = {
    "kvstore": "kvstore (RESP)",
    "redis": "Redis 7.2",
    "http": "kvstore (HTTP)",
    "router": "kvstore via router",
}


@dataclass(frozen=True, slots=True)
class Theme:
    name: str
    surface: str
    ink: str
    ink_secondary: str
    muted: str
    grid: str
    axis: str
    series: dict[str, str]


LIGHT = Theme(
    "light", "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7",
    {"kvstore": "#2a78d6", "redis": "#eb6834", "http": "#1baf7a", "router": "#eda100"},
)  # fmt: skip
DARK = Theme(
    "dark", "#1a1a19", "#ffffff", "#c3c2b7", "#898781", "#2c2c2a", "#383835",
    {"kvstore": "#3987e5", "redis": "#d95926", "http": "#199e70", "router": "#c98500"},
)  # fmt: skip

PERCENTILES = ("p50", "p75", "p90", "p99", "p99.9", "p99.99")


# ------------------------------------------------------------ selection
def entity(row: dict[str, Any]) -> str:
    if row["target"] == "kvstore" and row.get("protocol") == "http":
        return "http"
    return str(row["target"])


def find(rows: Iterable[dict[str, Any]], **match: Any) -> dict[str, Any] | None:
    for row in rows:
        if all(row.get(key) == value for key, value in match.items()):
            return row
    return None


def baseline(rows: list[dict[str, Any]], who: str) -> dict[str, Any] | None:
    """The shared baseline: 90% GET, fsync everysec, no pipelining."""
    target = "kvstore" if who == "http" else who
    protocol = "http" if who == "http" else "resp"
    return find(
        rows, target=target, protocol=protocol, fsync="everysec", read_ratio=0.9, pipeline=1
    )


def kops(value: float) -> str:
    if value >= 10_000:
        return f"{value / 1000:,.0f}k"
    if value >= 1000:
        return f"{value / 1000:.1f}".removesuffix(".0") + "k"
    return f"{value:,.0f}"


def ms(value_us: float) -> str:
    value = value_us / 1000
    return f"{value:,.0f}" if value >= 100 else f"{value:.1f}" if value >= 1 else f"{value:.2f}"


# --------------------------------------------------------------- charts
class Charts:
    def __init__(self, data: dict[str, Any], out_dir: Path) -> None:
        # Imported dynamically: matplotlib is an optional extra, and its numpy
        # stubs use syntax that mypy rejects when targeting Python 3.11.
        importlib.import_module("matplotlib").use("Agg")
        self._plt = importlib.import_module("matplotlib.pyplot")
        self.data = data
        self.rows: list[dict[str, Any]] = data["scenarios"]
        self.out_dir = out_dir

    def _figure(self, theme: Theme, ncols: int = 1, width: float = 8.0) -> tuple[Any, list[Any]]:
        plt = self._plt
        plt.rcParams.update(
            {
                "font.family": "sans-serif",
                "font.sans-serif": ["Segoe UI", "Helvetica", "Arial", "DejaVu Sans"],
                "font.size": 10,
                "text.color": theme.ink,
                "axes.labelcolor": theme.ink_secondary,
                "xtick.color": theme.muted,
                "ytick.color": theme.muted,
            }
        )
        fig, axes = plt.subplots(1, ncols, figsize=(width, 4.2), squeeze=False, sharey=ncols > 1)
        fig.patch.set_facecolor(theme.surface)
        for ax in axes[0]:
            ax.set_facecolor(theme.surface)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            for side in ("left", "bottom"):
                ax.spines[side].set_color(theme.axis)
                ax.spines[side].set_linewidth(1)
            ax.tick_params(length=0, labelsize=9)
            ax.grid(color=theme.grid, linewidth=1, linestyle="-")
            ax.set_axisbelow(True)
        return fig, list(axes[0])

    def _title(self, fig: Any, theme: Theme, title: str, subtitle: str) -> None:
        fig.text(0.012, 0.965, title, fontsize=13, fontweight="bold", color=theme.ink)
        fig.text(0.012, 0.905, subtitle, fontsize=9.5, color=theme.ink_secondary)
        fig.subplots_adjust(top=0.8, left=0.1, right=0.97, bottom=0.14)

    def _legend(self, ax: Any, theme: Theme, loc: str = "upper left") -> None:
        legend = ax.legend(frameon=False, fontsize=9, loc=loc, labelcolor=theme.ink)
        for handle in legend.legend_handles:
            handle.set_alpha(1)

    def _save(self, fig: Any, name: str, theme: Theme) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        fig.savefig(self.out_dir / f"{name}-{theme.name}.png", dpi=144, facecolor=theme.surface)
        self._plt.close(fig)

    def _hbar(self, ax: Any, theme: Theme, items: list[tuple[str, str, dict[str, Any]]]) -> None:
        """One horizontal bar per (label, entity, row), with the min-max spread as a whisker."""
        top = max(row["ops_per_sec_max"] for _, _, row in items)
        for i, (_label, who, row) in enumerate(items):
            y = len(items) - 1 - i
            value = row["ops_per_sec"]
            ax.barh(y, value, height=0.5, color=theme.series[who])
            ax.plot(
                [row["ops_per_sec_min"], row["ops_per_sec_max"]], [y, y],
                color=theme.ink_secondary, linewidth=1, solid_capstyle="butt",
            )  # fmt: skip
            ax.text(
                row["ops_per_sec_max"] + top * 0.015, y, f"{value:,.0f} ops/s",
                va="center", fontsize=9, color=theme.ink,
            )  # fmt: skip
        ax.set_yticks(range(len(items)), [label for label, _, _ in reversed(items)])
        ax.tick_params(axis="y", labelcolor=theme.ink_secondary, labelsize=10)
        ax.set_xlim(0, top * 1.28)
        ax.grid(axis="y", visible=False)
        ax.xaxis.set_major_formatter(lambda v, _: kops(v))

    # ---- 1. baseline throughput
    def baseline_throughput(self, theme: Theme) -> None:
        items = [(LABELS[w], w, r) for w in ENTITIES if (r := baseline(self.rows, w))]
        items.sort(key=lambda item: -item[2]["ops_per_sec"])
        fig, (ax,) = self._figure(theme)
        self._hbar(ax, theme, items)
        ax.set_xlabel("operations per second (median of runs; line = min to max)")
        self._title(
            fig, theme, "Throughput, same workload",
            "50 clients, 90% GET / 10% SET, 64 B values, fsync everysec, no pipelining",
        )  # fmt: skip
        fig.subplots_adjust(left=0.2)
        self._save(fig, "throughput", theme)

    # ---- 2. latency percentile ladder
    def latency_percentiles(self, theme: Theme) -> None:
        fig, (ax,) = self._figure(theme)
        xs = range(len(PERCENTILES))
        for who in ENTITIES:
            row = baseline(self.rows, who)
            if row is None:
                continue
            ys = [row["latency_us"][p] / 1000 for p in PERCENTILES]
            color = theme.series[who]
            ax.plot(xs, ys, color=color, linewidth=2, label=LABELS[who], solid_capstyle="round")
            ax.plot(xs, ys, "o", color=color, markersize=6, markeredgecolor=theme.surface,
                    markeredgewidth=2)  # fmt: skip
            ax.annotate(
                f"{ms(row['latency_us']['p99'] )} ms", (3, ys[3]), xytext=(6, -3),
                textcoords="offset points", fontsize=8.5, color=theme.ink_secondary,
            )  # fmt: skip
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:g}")
        ax.set_xticks(list(xs), list(PERCENTILES))
        ax.set_ylabel("latency, ms (log scale)")
        self._legend(ax, theme)
        self._title(
            fig, theme, "Latency percentiles, same workload",
            "Closed loop at full load; labels mark p99. From all runs' merged histograms",
        )  # fmt: skip
        self._save(fig, "latency-percentiles", theme)

    # ---- 3. pipelining
    def pipelining(self, theme: Theme) -> None:
        fig, (ax,) = self._figure(theme)
        depths = (1, 4, 16, 64)
        for who in ("kvstore", "redis"):
            points = [
                (d, row["ops_per_sec"])
                for d in depths
                if (row := find(self.rows, target=who, protocol="resp", fsync="everysec",
                                read_ratio=0.9, pipeline=d))
            ]  # fmt: skip
            if not points:
                continue
            xs, ys = zip(*points, strict=True)
            color = theme.series[who]
            ax.plot(xs, ys, color=color, linewidth=2, label=LABELS[who])
            ax.plot(xs, ys, "o", color=color, markersize=6, markeredgecolor=theme.surface,
                    markeredgewidth=2)  # fmt: skip
            ax.annotate(
                kops(ys[-1]), (xs[-1], ys[-1]), xytext=(7, -3), textcoords="offset points",
                fontsize=9, color=theme.ink,
            )  # fmt: skip
        ax.set_xscale("log", base=2)
        ax.set_xticks(depths, [str(d) for d in depths])
        ax.set_xlim(0.8, 100)
        ax.set_ylim(bottom=0)
        ax.yaxis.set_major_formatter(lambda v, _: kops(v))
        ax.set_xlabel("pipeline depth (commands per round trip)")
        ax.set_ylabel("operations per second")
        self._legend(ax, theme)
        self._title(
            fig, theme, "Pipelining",
            "50 clients, 90% GET, fsync everysec. At high depths Redis outruns the Python "
            "client; see the redis-benchmark table",
        )  # fmt: skip
        self._save(fig, "pipelining", theme)

    # ---- 4. fsync and group commit
    def fsync(self, theme: Theme) -> None:
        cases = [
            ("always", 1, "always"),
            ("everysec", 1, "everysec"),
            ("no", 1, "no"),
            ("always", 16, "always, pipeline 16"),
        ]
        fig, (ax,) = self._figure(theme)
        width = 0.36
        top = 0.0
        for offset, who in ((-width / 2, "kvstore"), (width / 2, "redis")):
            for i, (fsync, depth, _) in enumerate(cases):
                row = find(self.rows, target=who, protocol="resp", fsync=fsync, read_ratio=0.0,
                           pipeline=depth)  # fmt: skip
                if row is None:
                    continue
                value = row["ops_per_sec"]
                top = max(top, value)
                ax.bar(i + offset, value, width=width - 0.04, color=theme.series[who],
                       label=LABELS[who] if i == 0 else None)  # fmt: skip
                ax.text(i + offset, value, kops(value), ha="center", va="bottom", fontsize=8.5,
                        color=theme.ink)  # fmt: skip
        ax.set_xticks(range(len(cases)), [label for *_, label in cases])
        ax.tick_params(axis="x", labelcolor=theme.ink_secondary, labelsize=9.5)
        ax.set_ylim(0, top * 1.15)
        ax.grid(axis="x", visible=False)
        ax.yaxis.set_major_formatter(lambda v, _: kops(v))
        ax.set_ylabel("SETs per second")
        self._legend(ax, theme)
        self._title(
            fig, theme, "The price of durability",
            "100% SET, 50 clients, by appendfsync policy. The last pair pipelines 16 SETs",
        )  # fmt: skip
        self._save(fig, "fsync", theme)

    # ---- 5. read/write mix
    def workload(self, theme: Theme) -> None:
        fig, (ax,) = self._figure(theme)
        for who in ("kvstore", "redis"):
            points = sorted(
                (row["read_ratio"] * 100, row["ops_per_sec"])
                for row in self.rows
                if row["target"] == who and row["protocol"] == "resp"
                and row["fsync"] == "everysec" and row["pipeline"] == 1
            )  # fmt: skip
            if not points:
                continue
            xs, ys = zip(*points, strict=True)
            color = theme.series[who]
            ax.plot(xs, ys, color=color, linewidth=2, label=LABELS[who])
            ax.plot(xs, ys, "o", color=color, markersize=6, markeredgecolor=theme.surface,
                    markeredgewidth=2)  # fmt: skip
        ax.set_xlim(-3, 103)
        ax.set_ylim(bottom=0)
        ax.set_xticks([0, 5, 50, 90, 95], ["0%", "5%", "50%", "90%", "95%"])
        ax.yaxis.set_major_formatter(lambda v, _: kops(v))
        ax.set_xlabel("share of GETs (the rest are SETs)")
        ax.set_ylabel("operations per second")
        self._legend(ax, theme, loc="lower center")
        self._title(
            fig, theme, "Write-heavy to read-heavy",
            "50 clients, fsync everysec, no pipelining, uniform keys over 100k",
        )  # fmt: skip
        self._save(fig, "workload", theme)

    # ---- 6. latency vs offered load (open loop)
    def latency_curve(self, theme: Theme) -> None:
        curve = self.data.get("latency_curve") or []
        if not curve:
            return
        fig, axes = self._figure(theme, ncols=2, width=9.0)
        for ax, pct in zip(axes, ("p50", "p99"), strict=True):
            for who in ("kvstore", "redis"):
                points = [(p["offered_ops_per_sec"], p["latency_us"][pct] / 1000)
                          for p in curve if p["target"] == who]  # fmt: skip
                if not points:
                    continue
                xs, ys = zip(*points, strict=True)
                color = theme.series[who]
                ax.plot(xs, ys, color=color, linewidth=2, label=LABELS[who])
                ax.plot(xs, ys, "o", color=color, markersize=6, markeredgecolor=theme.surface,
                        markeredgewidth=2)  # fmt: skip
            ax.set_yscale("log")
            ax.yaxis.set_major_formatter(lambda v, _: f"{v:g}")
            ax.xaxis.set_major_formatter(lambda v, _: kops(v))
            ax.set_xlabel("offered load, ops/s")
            ax.set_title(pct, loc="left", fontsize=10.5, color=theme.ink, fontweight="bold")
        axes[0].set_ylabel("latency, ms (log scale)")
        self._legend(axes[0], theme)
        self._title(
            fig, theme, "Latency under a fixed offered load",
            "Open loop: requests scheduled at a fixed rate, latency counted from the scheduled "
            "send time",
        )  # fmt: skip
        fig.subplots_adjust(top=0.76, left=0.08, wspace=0.08)
        self._save(fig, "latency-vs-load", theme)

    def all(self) -> list[str]:
        charts: list[Callable[[Theme], None]] = [
            self.baseline_throughput,
            self.latency_percentiles,
            self.pipelining,
            self.fsync,
            self.workload,
            self.latency_curve,
        ]
        for chart in charts:
            for theme in (LIGHT, DARK):
                chart(theme)
        return sorted(path.name for path in self.out_dir.glob("*.png"))


# --------------------------------------------------------------- tables
def markdown_tables(data: dict[str, Any]) -> str:
    env, opts = data["environment"], data["options"]
    lines = [
        "# Benchmark results (generated)",
        "",
        "Generated by `python -m benchmarks.report` from "
        f"a run on {env['date']} (kvstore {env['kvstore_version']}, commit "
        f"`{env['git_commit'] or 'n/a'}`). Narrative and methodology: "
        "[BENCHMARKS.md](../BENCHMARKS.md).",
        "",
        "## Environment",
        "",
        "| | |",
        "|---|---|",
        f"| CPU | {env['cpu']} ({env['logical_cpus']} logical CPUs) |",
        f"| Memory | {env['memory_gb']} GB |",
        f"| OS | {env['platform']} |",
        f"| Python | {env['python']} ({env['event_loop']}) |",
        f"| Redis | {env.get('redis_version') or 'not run'} |",
        f"| Load average at start | {' '.join(env.get('load_average_at_start') or [])} |",
        f"| Runs | {opts['reps']} x {opts['duration_s']:g} s (+{opts['warmup_s']:g} s warmup), "
        f"{opts['clients']} clients, {opts['keyspace']:,} keys, {opts['value_size']} B values |",
        "",
        "## Closed-loop scenarios",
        "",
        "Throughput is the median of the runs (min-max in brackets). Latency is in ms, from "
        "the runs' merged histograms.",
        "",
        "| group | server | fsync | GET % | pipeline | ops/s | p50 | p99 | p99.9 | max |",
        "|---|---|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for row in data["scenarios"]:
        lat = row["latency_us"]
        lines.append(
            f"| {row['group']} | {LABELS[entity(row)]} | {row['fsync']} | "
            f"{row['read_ratio'] * 100:g} | {row['pipeline']} | "
            f"**{row['ops_per_sec']:,.0f}** ({row['ops_per_sec_min']:,.0f}-"
            f"{row['ops_per_sec_max']:,.0f}) | {ms(lat['p50'])} | {ms(lat['p99'])} | "
            f"{ms(lat['p99.9'])} | {ms(lat['max'])} |"
        )
    curve = data.get("latency_curve") or []
    if curve:
        lines += [
            "",
            "## Open loop: latency at a fixed offered load",
            "",
            "| server | offered ops/s | % of capacity | achieved ops/s | p50 | p99 | p99.9 |",
            "|---|--:|--:|--:|--:|--:|--:|",
        ]
        for p in curve:
            lat = p["latency_us"]
            lines.append(
                f"| {LABELS[p['target']]} | {p['offered_ops_per_sec']:,} | "
                f"{p['fraction'] * 100:.0f}% | {p['achieved_ops_per_sec']:,.0f} | "
                f"{ms(lat['p50'])} | {ms(lat['p99'])} | {ms(lat['p99.9'])} |"
            )
    bench = data.get("redis_benchmark") or []
    if bench:
        lines += [
            "",
            "## redis-benchmark",
            "",
            "`redis-benchmark -t set,get -c 50 -d 64 -r <keys> -P <depth>`: the C client, so "
            "these Redis numbers are not limited by a Python load generator.",
            "",
            "| server | test | pipeline | ops/s | p50 ms | p99 ms | max ms |",
            "|---|---|--:|--:|--:|--:|--:|",
        ]
        for r in bench:
            lines.append(
                f"| {LABELS[r['target']]} | {r['test']} | {r['pipeline']} | "
                f"{r['ops_per_sec']:,.0f} | {r['p50_ms']:g} | {r['p99_ms']:g} | {r['max_ms']:g} |"
            )
    pause = data.get("snapshot_pause") or []
    if pause:
        lines += [
            "",
            "## BGREWRITEAOF: event-loop pause by keyspace size",
            "",
            "`python -m benchmarks.pause`: the keyspace copy that replaces fork() blocks every "
            "command; the snapshot is then written by a background thread.",
            "",
            "| value type | keys | pause ms (median) | pause ms (max) | background write ms "
            "| snapshot MB |",
            "|---|--:|--:|--:|--:|--:|",
        ]
        for r in pause:
            lines.append(
                f"| {r['kind']} | {r['keys']:,} | {r['pause_ms']:,.1f} | {r['pause_ms_max']:,.1f} "
                f"| {r['background_write_ms']:,.0f} | {r['snapshot_bytes'] / 1e6:,.1f} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmarks.report", description=__doc__)
    p.add_argument("results", type=Path, help="JSON written by benchmarks.suite")
    p.add_argument("--out-dir", type=Path, default=Path("docs/benchmarks"))
    p.add_argument("--no-charts", action="store_true", help="tables only (no matplotlib needed)")
    args = p.parse_args(argv)

    data = json.loads(args.results.read_text(encoding="utf-8"))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    tables = args.out_dir / "results.md"
    tables.write_text(markdown_tables(data), encoding="utf-8")
    print(f"wrote {tables}")
    if not args.no_charts:
        for name in Charts(data, args.out_dir).all():
            print(f"wrote {args.out_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
