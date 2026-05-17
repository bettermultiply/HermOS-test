#!/usr/bin/env python3
import csv
import math
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[2]
SUMMARY_DIR = ROOT / "data" / "summary"
FIGURE_DIR = ROOT / "data" / "figures"

FILTERS = {
    "phase": "workload",
    "metric_kind": "block",
}

PAIR_INPUTS = [
    ("same_task", SUMMARY_DIR / "same_task_run_pairs.csv"),
    ("cross_task", SUMMARY_DIR / "cross_task_run_pairs.csv"),
    ("all_task", SUMMARY_DIR / "all_task_run_pairs.csv"),
]

DRIVE_IDS = ["rootfs", "workspace"]

PAIR_COLORS = {
    "same_task-rootfs": "#1f77b4",
    "same_task-workspace": "#ff7f0e",
    "cross_task-rootfs": "#2ca02c",
    "cross_task-workspace": "#d62728",
    "all_task-rootfs": "#9467bd",
    "all_task-workspace": "#8c564b",
}

DRIVE_COLORS = {
    "rootfs": "#1f77b4",
    "workspace": "#ff7f0e",
}


def read_csv(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def matches_filters(row):
    return all(row.get(field) == expected for field, expected in FILTERS.items())


def parse_float(row, field):
    try:
        return float(row[field])
    except (KeyError, TypeError, ValueError):
        return None


def parse_int(row, field):
    try:
        return int(float(row[field]))
    except (KeyError, TypeError, ValueError):
        return None


def quantile(sorted_values, q):
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return sorted_values[low]
    fraction = position - low
    return sorted_values[low] * (1.0 - fraction) + sorted_values[high] * fraction


def format_count(count):
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


class Svg:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
        ]

    def add(self, content):
        self.parts.append(content)

    def line(self, x1, y1, x2, y2, **attrs):
        self.add(
            f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" {attr_string(attrs)}/>'
        )

    def rect(self, x, y, width, height, **attrs):
        self.add(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{width:.2f}" height="{height:.2f}" {attr_string(attrs)}/>'
        )

    def circle(self, cx, cy, r, **attrs):
        self.add(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" {attr_string(attrs)}/>')

    def path(self, d, **attrs):
        self.add(f'<path d="{d}" {attr_string(attrs)}/>')

    def text(self, x, y, text, **attrs):
        self.add(f'<text x="{x:.2f}" y="{y:.2f}" {attr_string(attrs)}>{escape(str(text))}</text>')

    def finish(self):
        self.parts.append("</svg>")
        return "\n".join(self.parts) + "\n"


def attr_string(attrs):
    normalized = []
    for key, value in attrs.items():
        normalized_key = key.replace("_", "-")
        normalized.append(f'{normalized_key}="{escape(str(value))}"')
    return " ".join(normalized)


def make_ecdf(values):
    sorted_values = sorted(value for value in values if 0.0 <= value <= 1.0)
    count = len(sorted_values)
    if count == 0:
        return []

    points = [(0.0, 0.0)]
    for index, value in enumerate(sorted_values, start=1):
        previous = (index - 1) / count
        current = index / count
        points.append((value, previous))
        points.append((value, current))
    points.append((1.0, 1.0))
    return points


def load_pair_series(metric):
    series = {}
    for relation, path in PAIR_INPUTS:
        rows = read_csv(path)
        by_drive = defaultdict(list)
        for row in rows:
            if not matches_filters(row):
                continue
            drive_id = row.get("drive_id")
            if drive_id not in DRIVE_IDS:
                continue
            value = parse_float(row, metric)
            if value is None:
                continue
            by_drive[drive_id].append(value)
        for drive_id in DRIVE_IDS:
            series[f"{relation}-{drive_id}"] = by_drive[drive_id]
    return series


def render_ecdf(metric, title, output_path):
    width = 1100
    height = 720
    left = 92
    right = 310
    top = 82
    bottom = 96
    plot_width = width - left - right
    plot_height = height - top - bottom
    legend_x = width - right + 42
    legend_y = top + 30

    def x_scale(value):
        return left + value * plot_width

    def y_scale(value):
        return top + (1.0 - value) * plot_height

    svg = Svg(width, height)
    svg.rect(0, 0, width, height, fill="#ffffff")
    svg.text(
        left,
        42,
        title,
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=26,
        font_weight=700,
        fill="#111827",
    )
    svg.text(
        left,
        66,
        "phase = workload, metric_kind = block",
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=14,
        fill="#4b5563",
    )

    svg.rect(left, top, plot_width, plot_height, fill="#fbfbf8", stroke="#d1d5db", stroke_width=1.2)

    for tick in [i / 10 for i in range(11)]:
        x = x_scale(tick)
        y = y_scale(tick)
        stroke = "#e5e7eb" if 0.0 < tick < 1.0 else "#d1d5db"
        svg.line(x, top, x, top + plot_height, stroke=stroke, stroke_width=1)
        svg.line(left, y, left + plot_width, y, stroke=stroke, stroke_width=1)
        svg.text(
            x,
            top + plot_height + 24,
            f"{tick:.1f}",
            text_anchor="middle",
            font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
            font_size=13,
            fill="#374151",
        )
        svg.text(
            left - 14,
            y + 4,
            f"{tick:.1f}",
            text_anchor="end",
            font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
            font_size=13,
            fill="#374151",
        )

    svg.line(left, top + plot_height, left + plot_width, top + plot_height, stroke="#111827", stroke_width=1.4)
    svg.line(left, top, left, top + plot_height, stroke="#111827", stroke_width=1.4)

    series = load_pair_series(metric)
    for label, color in PAIR_COLORS.items():
        points = make_ecdf(series.get(label, []))
        if not points:
            continue
        commands = [f"M {x_scale(points[0][0]):.2f} {y_scale(points[0][1]):.2f}"]
        commands.extend(f"L {x_scale(x):.2f} {y_scale(y):.2f}" for x, y in points[1:])
        svg.path(
            " ".join(commands),
            fill="none",
            stroke=color,
            stroke_width=2.8,
            stroke_linecap="round",
            stroke_linejoin="round",
        )

    svg.text(
        left + plot_width / 2,
        height - 34,
        metric,
        text_anchor="middle",
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=17,
        fill="#111827",
    )
    svg.text(
        28,
        top + plot_height / 2,
        "Empirical cumulative probability",
        text_anchor="middle",
        transform=f"rotate(-90 28 {top + plot_height / 2:.2f})",
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=17,
        fill="#111827",
    )

    svg.text(
        legend_x,
        legend_y - 20,
        "Relation / drive",
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=16,
        font_weight=700,
        fill="#111827",
    )
    for index, (label, color) in enumerate(PAIR_COLORS.items()):
        y = legend_y + index * 36
        count = len(series.get(label, []))
        svg.line(legend_x, y, legend_x + 36, y, stroke=color, stroke_width=3.8, stroke_linecap="round")
        svg.text(
            legend_x + 48,
            y + 5,
            f"{label}  n={count}",
            font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
            font_size=14,
            fill="#1f2937",
        )

    summary_y = legend_y + 250
    svg.text(
        legend_x,
        summary_y,
        "Median / p90",
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=16,
        font_weight=700,
        fill="#111827",
    )
    for index, (label, color) in enumerate(PAIR_COLORS.items()):
        values = sorted(series.get(label, []))
        if not values:
            text = f"{label}: empty"
        else:
            text = f"{label}: {quantile(values, 0.5):.3f} / {quantile(values, 0.9):.3f}"
        svg.text(
            legend_x,
            summary_y + 28 + index * 24,
            text,
            font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
            font_size=12.5,
            fill=color,
        )

    output_path.write_text(svg.finish())


def load_scale_points():
    rows = read_csv(SUMMARY_DIR / "same_task_scale.csv")
    points = []
    for row in rows:
        if not matches_filters(row):
            continue
        drive_id = row.get("drive_id")
        if drive_id not in DRIVE_IDS:
            continue
        union_count = parse_int(row, "union_count")
        core_count = parse_int(row, "core_count")
        if union_count is None or core_count is None or union_count <= 0:
            continue
        points.append(
            {
                "task_id": row.get("task_id", ""),
                "drive_id": drive_id,
                "union_count": union_count,
                "core_count": core_count,
            }
        )
    return points


def log_ticks(min_value, max_value):
    min_exp = math.floor(math.log10(min_value))
    max_exp = math.ceil(math.log10(max_value))
    ticks = []
    for exp in range(min_exp, max_exp + 1):
        for multiplier in [1, 2, 5]:
            value = multiplier * (10**exp)
            if min_value <= value <= max_value:
                ticks.append(value)
    return ticks


def render_stability_scatter(output_path):
    points = load_scale_points()
    if not points:
        raise RuntimeError("No same_task_scale rows matched phase=workload and metric_kind=block")

    min_union = min(point["union_count"] for point in points)
    max_union = max(point["union_count"] for point in points)
    max_core = max(point["core_count"] for point in points)
    x_min = 10 ** math.floor(math.log10(max(1, min_union)))
    x_max = 10 ** math.ceil(math.log10(max_union))
    y_min = 0
    y_max = max(max_core, max_union)
    y_max = int(math.ceil(y_max * 1.06))

    width = 1050
    height = 720
    left = 104
    right = 268
    top = 82
    bottom = 96
    plot_width = width - left - right
    plot_height = height - top - bottom
    legend_x = width - right + 42
    legend_y = top + 40

    log_x_min = math.log10(x_min)
    log_x_max = math.log10(x_max)

    def x_scale(value):
        return left + (math.log10(value) - log_x_min) / (log_x_max - log_x_min) * plot_width

    def y_scale(value):
        return top + (1.0 - (value - y_min) / (y_max - y_min)) * plot_height

    svg = Svg(width, height)
    svg.rect(0, 0, width, height, fill="#ffffff")
    svg.text(
        left,
        42,
        "Same-task Stability: Core vs Union Block Set Size",
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=25,
        font_weight=700,
        fill="#111827",
    )
    svg.text(
        left,
        66,
        "phase = workload, metric_kind = block; x-axis uses log scale",
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=14,
        fill="#4b5563",
    )
    svg.rect(left, top, plot_width, plot_height, fill="#fbfbf8", stroke="#d1d5db", stroke_width=1.2)

    for tick in log_ticks(x_min, x_max):
        major = str(tick).startswith("1")
        svg.line(
            x_scale(tick),
            top,
            x_scale(tick),
            top + plot_height,
            stroke="#d1d5db" if major else "#eceff3",
            stroke_width=1.0 if major else 0.7,
        )
        if major:
            svg.text(
                x_scale(tick),
                top + plot_height + 24,
                format_count(tick),
                text_anchor="middle",
                font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
                font_size=13,
                fill="#374151",
            )

    y_step = nice_step(y_max / 6)
    y_tick = 0
    while y_tick <= y_max:
        svg.line(left, y_scale(y_tick), left + plot_width, y_scale(y_tick), stroke="#e5e7eb", stroke_width=1)
        svg.text(
            left - 14,
            y_scale(y_tick) + 4,
            format_count(y_tick),
            text_anchor="end",
            font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
            font_size=13,
            fill="#374151",
        )
        y_tick += y_step

    svg.line(left, top + plot_height, left + plot_width, top + plot_height, stroke="#111827", stroke_width=1.4)
    svg.line(left, top, left, top + plot_height, stroke="#111827", stroke_width=1.4)

    identity = []
    samples = 180
    for index in range(samples + 1):
        log_value = log_x_min + (log_x_max - log_x_min) * index / samples
        value = 10**log_value
        if y_min <= value <= y_max:
            identity.append((x_scale(value), y_scale(value)))
    if identity:
        commands = [f"M {identity[0][0]:.2f} {identity[0][1]:.2f}"]
        commands.extend(f"L {x:.2f} {y:.2f}" for x, y in identity[1:])
        svg.path(" ".join(commands), fill="none", stroke="#6b7280", stroke_width=2, stroke_dasharray="7 6")
        svg.text(
            x_scale(max(x_min, min(x_max, y_max * 0.72))),
            y_scale(max(x_min, min(y_max, y_max * 0.72))) - 10,
            "y = x",
            font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
            font_size=13,
            fill="#4b5563",
        )

    for drive_id in DRIVE_IDS:
        for point in [item for item in points if item["drive_id"] == drive_id]:
            svg.circle(
                x_scale(point["union_count"]),
                y_scale(point["core_count"]),
                5.2,
                fill=DRIVE_COLORS[drive_id],
                fill_opacity=0.72,
                stroke="#ffffff",
                stroke_width=0.9,
            )

    svg.text(
        left + plot_width / 2,
        height - 34,
        "union_count (log scale)",
        text_anchor="middle",
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=17,
        fill="#111827",
    )
    svg.text(
        30,
        top + plot_height / 2,
        "core_count",
        text_anchor="middle",
        transform=f"rotate(-90 30 {top + plot_height / 2:.2f})",
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=17,
        fill="#111827",
    )

    svg.text(
        legend_x,
        legend_y - 18,
        "Drive",
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=16,
        font_weight=700,
        fill="#111827",
    )
    for index, drive_id in enumerate(DRIVE_IDS):
        y = legend_y + index * 38
        count = sum(1 for point in points if point["drive_id"] == drive_id)
        svg.circle(legend_x + 12, y - 4, 5.5, fill=DRIVE_COLORS[drive_id], fill_opacity=0.72, stroke="#ffffff")
        svg.text(
            legend_x + 30,
            y,
            f"{drive_id}  n={count}",
            font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
            font_size=14,
            fill="#1f2937",
        )

    svg.text(
        legend_x,
        legend_y + 120,
        "Interpretation",
        font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
        font_size=16,
        font_weight=700,
        fill="#111827",
    )
    notes = [
        "Each point is one",
        "task_id + drive_id.",
        "Near y = x means",
        "the stable core is",
        "close to the total",
        "accessed block set.",
    ]
    for index, note in enumerate(notes):
        svg.text(
            legend_x,
            legend_y + 148 + index * 21,
            note,
            font_family="DejaVu Sans, Liberation Sans, Arial, sans-serif",
            font_size=13,
            fill="#4b5563",
        )

    output_path.write_text(svg.finish())


def nice_step(raw_step):
    if raw_step <= 0:
        return 1
    exponent = math.floor(math.log10(raw_step))
    fraction = raw_step / (10**exponent)
    if fraction <= 1:
        nice_fraction = 1
    elif fraction <= 2:
        nice_fraction = 2
    elif fraction <= 5:
        nice_fraction = 5
    else:
        nice_fraction = 10
    return int(nice_fraction * (10**exponent))


def export_png(svg_path):
    converter = shutil.which("rsvg-convert")
    if converter is None:
        return None
    png_path = svg_path.with_suffix(".png")
    subprocess.run([converter, "-o", str(png_path), str(svg_path)], check=True)
    return png_path


def main():
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    outputs = []

    specs = [
        ("high_cover", "Pair Similarity ECDF: High Cover"),
        ("jaccard", "Pair Similarity ECDF: Jaccard"),
    ]
    for metric, title in specs:
        svg_path = FIGURE_DIR / f"pair_similarity_{metric}_ecdf.svg"
        render_ecdf(metric, title, svg_path)
        outputs.append(svg_path)

    scatter_path = FIGURE_DIR / "same_task_stability_scatter.svg"
    render_stability_scatter(scatter_path)
    outputs.append(scatter_path)

    png_outputs = []
    for svg_path in outputs:
        png_path = export_png(svg_path)
        if png_path is not None:
            png_outputs.append(png_path)

    for path in outputs + png_outputs:
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
