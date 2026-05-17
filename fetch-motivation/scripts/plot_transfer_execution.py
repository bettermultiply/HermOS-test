#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import math
import shutil
import statistics
import subprocess as sp
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "data" / "experiment_runs.csv"
DEFAULT_OUT = ROOT / "data" / "figures" / "transfer_execution_breakdown"
DEFAULT_WORKLOADS = ["health-exec", "read-list", "agent-tool-replay"]

WORKLOAD_TITLES = {
    "health-daemon": "Lightweight health check",
    "health-exec": "Lightweight health check",
    "read-list": "Memory-intensive read-list",
    "agent-tool-replay": "General agent workload",
}

GROUPS = [
    ("eager-local", "Eager / local", "#2f6f9f", "solid"),
    ("lazy-local", "Lazy / local", "#c95722", "solid"),
    ("eager-remote", "Eager / remote", "#2f6f9f", "remote"),
    ("lazy-remote-dedup", "Lazy / remote", "#c95722", "dash"),
]


@dataclass(frozen=True)
class Point:
    concurrency: int
    transfer_s: float
    execution_s: float
    total_s: float
    sample_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plot transfer + execution time across concurrency for the "
            "fetch-motivation experiment."
        )
    )
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Input experiment_runs.csv path.")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="Output path without extension, or a .svg path.",
    )
    parser.add_argument(
        "--workloads",
        nargs="+",
        default=DEFAULT_WORKLOADS,
        help="Three workload ids to plot, in subplot order.",
    )
    parser.add_argument(
        "--formats",
        nargs="+",
        default=["svg", "png", "pdf"],
        choices=["svg", "png", "pdf"],
        help="Output formats. PNG/PDF require rsvg-convert.",
    )
    parser.add_argument(
        "--title",
        default="Transfer cost versus lazy fault accumulation",
        help="Figure title.",
    )
    parser.add_argument(
        "--yscale",
        default="log",
        choices=["log", "linear"],
        help="Y-axis scale. Log is the default; linear remains available for comparison.",
    )
    return parser.parse_args()


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    return statistics.median(values)


def load_points(csv_path: Path) -> dict[str, dict[str, list[Point]]]:
    buckets: dict[tuple[str, str, int], list[tuple[float, float, float]]] = {}
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        required = {
            "group_name",
            "concurrency",
            "workload_id",
            "memory_pull_ms",
            "snapshot_state_pull_ms",
            "workload_ms_avg",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"{csv_path} is missing columns: {', '.join(sorted(missing))}")

        for row in reader:
            group = row["group_name"]
            workload = row["workload_id"]
            concurrency = int(row["concurrency"])
            transfer_ms = float(row["memory_pull_ms"]) + float(row["snapshot_state_pull_ms"])
            execution_ms = float(row["workload_ms_avg"])
            total_ms = transfer_ms + execution_ms
            buckets.setdefault((workload, group, concurrency), []).append(
                (transfer_ms / 1000.0, execution_ms / 1000.0, total_ms / 1000.0)
            )

    points: dict[str, dict[str, list[Point]]] = {}
    for (workload, group, concurrency), samples in buckets.items():
        transfers = [sample[0] for sample in samples]
        executions = [sample[1] for sample in samples]
        totals = [sample[2] for sample in samples]
        points.setdefault(workload, {}).setdefault(group, []).append(
            Point(
                concurrency=concurrency,
                transfer_s=median(transfers),
                execution_s=median(executions),
                total_s=median(totals),
                sample_count=len(samples),
            )
        )

    for workload_groups in points.values():
        for group_points in workload_groups.values():
            group_points.sort(key=lambda p: p.concurrency)
    return points


def nice_upper(value: float) -> float:
    if value <= 0:
        return 1.0
    value *= 1.08
    exponent = math.floor(math.log10(value))
    fraction = value / (10**exponent)
    for step in (1.0, 1.5, 2.0, 2.5, 5.0, 7.5, 10.0):
        if fraction <= step:
            return step * (10**exponent)
    return 10.0 * (10**exponent)


def ticks_for(max_value: float) -> list[float]:
    top = nice_upper(max_value)
    return [top * i / 4 for i in range(5)]


def log_bounds(values: list[float]) -> tuple[float, float]:
    positives = [value for value in values if value > 0]
    if not positives:
        return 0.001, 1.0
    low = min(positives) * 0.8
    high = max(positives) * 1.15
    low_power = math.floor(math.log10(low))
    high_power = math.ceil(math.log10(high))
    return 10**low_power, 10**high_power


def log_ticks(low: float, high: float) -> list[float]:
    ticks: list[float] = []
    low_power = math.floor(math.log10(low))
    high_power = math.ceil(math.log10(high))
    for power in range(low_power, high_power + 1):
        for base in (1.0, 2.0, 5.0):
            value = base * (10**power)
            if low <= value <= high:
                ticks.append(value)
    return ticks


def fmt_tick(value: float) -> str:
    if value == 0:
        return "0"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.0f}"
    if value >= 1:
        return f"{value:.1f}"
    return f"{value:.2f}"


def svg_line(x1: float, y1: float, x2: float, y2: float, **attrs: str | float) -> str:
    attrs_s = attrs_to_str({"x1": x1, "y1": y1, "x2": x2, "y2": y2, **attrs})
    return f"<line {attrs_s}/>"


def svg_rect(x: float, y: float, width: float, height: float, **attrs: str | float) -> str:
    attrs_s = attrs_to_str({"x": x, "y": y, "width": width, "height": height, **attrs})
    return f"<rect {attrs_s}/>"


def svg_text(x: float, y: float, text: str, **attrs: str | float) -> str:
    attrs_s = attrs_to_str({"x": x, "y": y, **attrs})
    return f"<text {attrs_s}>{html.escape(text)}</text>"


def svg_path(points: list[tuple[float, float]], **attrs: str | float) -> str:
    if not points:
        return ""
    d = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in points)
    attrs_s = attrs_to_str({"d": d, **attrs})
    return f"<path {attrs_s}/>"


def attrs_to_str(attrs: dict[str, str | float | int]) -> str:
    parts = []
    for name, value in attrs.items():
        if value is None:
            continue
        if name.endswith("_"):
            attr_name = name[:-1]
        else:
            attr_name = name.replace("_", "-")
        parts.append(f'{attr_name}="{html.escape(str(value), quote=True)}"')
    return " ".join(parts)


def marker(x: float, y: float, color: str) -> str:
    return (
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.6" fill="white" '
        f'stroke="{color}" stroke-width="2"/>'
    )


def render_svg(
    points: dict[str, dict[str, list[Point]]],
    workloads: list[str],
    title: str,
    yscale: str,
) -> str:
    width = 1440
    height = 560
    margin_left = 78
    margin_right = 34
    plot_top = 122
    plot_bottom = 458
    plot_gap = 44
    panel_w = (width - margin_left - margin_right - plot_gap * 2) / 3
    panel_h = plot_bottom - plot_top

    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text { font-family: DejaVu Sans, Liberation Sans, Arial, sans-serif; fill: #1f2933; }",
        ".title { font-size: 24px; font-weight: 700; }",
        ".subtitle { font-size: 13px; fill: #52616f; }",
        ".panel-title { font-size: 16px; font-weight: 700; }",
        ".axis { stroke: #2f3a45; stroke-width: 1.2; }",
        ".grid { stroke: #d8dee6; stroke-width: 1; }",
        ".tick { font-size: 11px; fill: #52616f; }",
        ".label { font-size: 13px; fill: #2f3a45; }",
        ".legend { font-size: 12px; fill: #2f3a45; }",
        "</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        svg_text(40, 42, title, class_="title"),
        svg_text(
            40,
            67,
            "Y = memory/state transfer + average guest workload execution; restoration/sandbox startup is excluded. Lines show medians across repeats.",
            class_="subtitle",
        ),
    ]

    legend_x = 48
    legend_y = 91
    for idx, (_group, label, color, style) in enumerate(GROUPS):
        x = legend_x + idx * 160
        opacity = 0.62 if style in {"remote", "dash"} else 0.95
        out.append(svg_rect(x, legend_y - 10, 34, 16, fill=color, opacity=opacity, stroke=color, stroke_width=1.2))
        out.append(svg_text(x + 44, legend_y + 4, label, class_="legend"))
    transfer_legend_x = legend_x + len(GROUPS) * 160 + 18
    out.append(
        f'<rect x="{transfer_legend_x}" y="{legend_y - 9}" width="32" height="14" '
        'fill="#2f6f9f" opacity="0.18" stroke="#2f6f9f" stroke-width="1"/>'
    )
    out.append(svg_text(transfer_legend_x + 40, legend_y + 4, "Eager transfer portion", class_="legend"))

    for panel_idx, workload in enumerate(workloads):
        x0 = margin_left + panel_idx * (panel_w + plot_gap)
        y0 = plot_top
        y_axis_x = x0
        x_axis_y = plot_bottom
        workload_points = points.get(workload, {})
        plotted_groups = {group for group, _label, _color, _style in GROUPS}
        concurrency_values = sorted(
            {
                point.concurrency
                for group, group_points in workload_points.items()
                if group in plotted_groups
                for point in group_points
            }
        )
        if not concurrency_values:
            out.append(svg_text(x0 + 20, y0 + 40, f"No data for {workload}", class_="panel-title"))
            continue

        y_values = [
            point.total_s
            for group, group_points in workload_points.items()
            if group in plotted_groups
            for point in group_points
        ]
        for group, _label, _color, _style in GROUPS:
            if group.startswith("eager"):
                y_values.extend(
                    point.execution_s
                    for point in workload_points.get(group, [])
                    if point.execution_s > 0
                )

        if yscale == "log":
            y_low_value, y_top_value = log_bounds(y_values)
            tick_values = log_ticks(y_low_value, y_top_value)
        else:
            y_low_value = 0.0
            tick_values = ticks_for(max(y_values))
            y_top_value = tick_values[-1]

        def sy(value: float) -> float:
            if yscale == "log":
                safe_value = max(value, y_low_value)
                span = math.log10(y_top_value) - math.log10(y_low_value)
                return x_axis_y - ((math.log10(safe_value) - math.log10(y_low_value)) / span) * panel_h
            return x_axis_y - (value / y_top_value) * panel_h

        title_text = WORKLOAD_TITLES.get(workload, workload)
        out.append(svg_text(x0, y0 - 15, f"{title_text} ({workload})", class_="panel-title"))
        out.append(svg_line(y_axis_x, y0, y_axis_x, x_axis_y, class_="axis"))
        out.append(svg_line(y_axis_x, x_axis_y, x0 + panel_w, x_axis_y, class_="axis"))

        for tick in tick_values:
            y = sy(tick)
            out.append(svg_line(y_axis_x, y, x0 + panel_w, y, class_="grid"))
            out.append(svg_text(y_axis_x - 10, y + 4, fmt_tick(tick), class_="tick", text_anchor="end"))

        group_slot = (panel_w - 36) / max(1, len(concurrency_values))

        for group_idx, concurrency in enumerate(concurrency_values):
            group_center = x0 + 18 + group_idx * group_slot + group_slot / 2
            out.append(svg_line(group_center, x_axis_y, group_center, x_axis_y + 5, stroke="#2f3a45", stroke_width=1))
            out.append(svg_text(group_center, x_axis_y + 22, str(concurrency), class_="tick", text_anchor="middle"))

        out.append(svg_text(x0 + panel_w / 2, height - 48, "Concurrency", class_="label", text_anchor="middle"))
        if panel_idx == 0:
            out.append(
                '<text x="22" y="290" class="label" text-anchor="middle" '
                f'transform="rotate(-90 22 290)">Transfer + execution time (s'
                f'{", log scale" if yscale == "log" else ""})</text>'
            )

        bar_gap = 2.4
        bar_w = min(13.0, (group_slot - 8) / len(GROUPS) - bar_gap)
        point_by_group = {
            group: {point.concurrency: point for point in workload_points.get(group, [])}
            for group, _label, _color, _style in GROUPS
        }

        for concurrency_idx, concurrency in enumerate(concurrency_values):
            group_left = x0 + 18 + concurrency_idx * group_slot + (group_slot - (bar_w + bar_gap) * len(GROUPS)) / 2
            for bar_idx, (group, _label, color, style) in enumerate(GROUPS):
                point = point_by_group.get(group, {}).get(concurrency)
                if point is None:
                    continue
                bar_x = group_left + bar_idx * (bar_w + bar_gap)
                total_y = sy(point.total_s)
                base_y = x_axis_y if yscale == "linear" else sy(y_low_value)
                bar_h = max(0.8, base_y - total_y)
                opacity = 0.62 if style in {"remote", "dash"} else 0.95
                out.append(
                    svg_rect(
                        bar_x,
                        total_y,
                        bar_w,
                        bar_h,
                        fill=color,
                        opacity=opacity,
                        stroke=color,
                        stroke_width=0.9,
                    )
                )
                if group.startswith("eager") and point.transfer_s > 0:
                    execution_y = sy(point.execution_s)
                    transfer_h = max(0.8, execution_y - total_y)
                    out.append(
                        svg_rect(
                            bar_x,
                            total_y,
                            bar_w,
                            transfer_h,
                            fill="#ffffff",
                            opacity=0.38,
                            stroke=color,
                            stroke_width=0.7,
                        )
                    )

    out.append("</svg>")
    return "\n".join(out)


def write_outputs(svg: str, out_arg: str, formats: list[str]) -> list[Path]:
    out_path = Path(out_arg)
    base = out_path.with_suffix("") if out_path.suffix else out_path
    base.parent.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    svg_path = base.with_suffix(".svg")
    if "svg" in formats or any(fmt in formats for fmt in ("png", "pdf")):
        svg_path.write_text(svg)
        written.append(svg_path)

    if any(fmt in formats for fmt in ("png", "pdf")):
        converter = shutil.which("rsvg-convert")
        if not converter:
            missing = [fmt for fmt in ("png", "pdf") if fmt in formats]
            print(f"warning: rsvg-convert not found; skipped {', '.join(missing)} export")
            return written
        for fmt in ("png", "pdf"):
            if fmt not in formats:
                continue
            target = base.with_suffix(f".{fmt}")
            sp.run([converter, "-f", fmt, "-o", str(target), str(svg_path)], check=True)
            written.append(target)

    return written


def main() -> int:
    args = parse_args()
    if len(args.workloads) != 3:
        raise SystemExit("--workloads must contain exactly three workload ids")
    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"input CSV not found: {csv_path}")

    points = load_points(csv_path)
    missing = [workload for workload in args.workloads if workload not in points]
    if missing:
        raise SystemExit(f"no data for workload(s): {', '.join(missing)}")

    svg = render_svg(points, args.workloads, args.title, args.yscale)
    written = write_outputs(svg, args.out, args.formats)
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
