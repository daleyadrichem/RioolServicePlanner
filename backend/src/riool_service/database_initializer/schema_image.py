"""SVG schema image generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from riool_service.database.models.base import Base


def generate_database_schema_image(
    *,
    output_path: str | Path = "database_schema.svg",
) -> Path:
    """Generate an SVG ER-style image from the SQLAlchemy metadata."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    tables = sorted(Base.metadata.tables.values(), key=lambda table: table.name)
    if not tables:
        raise RuntimeError("No tables were registered in Base.metadata.")

    box_width = 310
    header_height = 34
    row_height = 22
    box_gap_x = 90
    box_gap_y = 70
    columns_per_row = 3
    margin = 40

    table_layout: dict[str, dict[str, int]] = {}
    row_heights: dict[int, int] = {}

    for index, table in enumerate(tables):
        row = index // columns_per_row
        height = header_height + row_height * max(1, len(table.columns)) + 14
        row_heights[row] = max(row_heights.get(row, 0), height)

    row_y: dict[int, int] = {}
    current_y = margin
    for row in range((len(tables) + columns_per_row - 1) // columns_per_row):
        row_y[row] = current_y
        current_y += row_heights[row] + box_gap_y

    for index, table in enumerate(tables):
        row = index // columns_per_row
        col = index % columns_per_row
        x = margin + col * (box_width + box_gap_x)
        y = row_y[row]
        height = header_height + row_height * max(1, len(table.columns)) + 14
        table_layout[table.name] = {
            "x": x,
            "y": y,
            "width": box_width,
            "height": height,
        }

    svg_width = (
        margin * 2 + columns_per_row * box_width + (columns_per_row - 1) * box_gap_x
    )
    svg_height = current_y + margin

    def column_label(column: Any) -> str:
        markers: list[str] = []
        if column.primary_key:
            markers.append("PK")
        if column.foreign_keys:
            markers.append("FK")

        prefix = f"[{', '.join(markers)}] " if markers else ""
        nullable = "" if column.nullable or column.primary_key else " NOT NULL"
        return f"{prefix}{column.name}: {column.type}{nullable}"

    def table_anchor(table_name: str, side: str) -> tuple[int, int]:
        box = table_layout[table_name]
        x = box["x"] if side == "left" else box["x"] + box["width"]
        y = box["y"] + box["height"] // 2
        return x, y

    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_width}" height="{svg_height}" viewBox="0 0 {svg_width} {svg_height}">',
        "<defs>",
        '<marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">',
        '<path d="M0,0 L0,6 L9,3 z" fill="#475569" />',
        "</marker>",
        "</defs>",
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<text x="40" y="26" font-family="Arial, sans-serif" font-size="18" font-weight="700" fill="#0f172a">Database schema</text>',
    ]

    for table in tables:
        for column in table.columns:
            for foreign_key in column.foreign_keys:
                source_name = table.name
                target_name = foreign_key.column.table.name
                if source_name not in table_layout or target_name not in table_layout:
                    continue

                source_box = table_layout[source_name]
                target_box = table_layout[target_name]
                source_side = "left" if target_box["x"] < source_box["x"] else "right"
                target_side = "right" if source_side == "left" else "left"
                x1, y1 = table_anchor(source_name, source_side)
                x2, y2 = table_anchor(target_name, target_side)
                mid_x = (x1 + x2) // 2

                parts.append(
                    "<path "
                    f'd="M{x1},{y1} C{mid_x},{y1} {mid_x},{y2} {x2},{y2}" '
                    'fill="none" stroke="#475569" stroke-width="1.6" '
                    'marker-end="url(#arrow)" opacity="0.75"/>'
                )

    for table in tables:
        box = table_layout[table.name]
        x = box["x"]
        y = box["y"]
        width = box["width"]
        height = box["height"]

        parts.extend(
            [
                f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="10" fill="#ffffff" stroke="#cbd5e1"/>',
                f'<rect x="{x}" y="{y}" width="{width}" height="{header_height}" rx="10" fill="#1e293b"/>',
                f'<path d="M{x},{y + header_height - 10} H{x + width} V{y + header_height} H{x} Z" fill="#1e293b"/>',
                f'<text x="{x + 14}" y="{y + 23}" font-family="Arial, sans-serif" font-size="14" font-weight="700" fill="#ffffff">{escape(table.name)}</text>',
            ]
        )

        columns = list(table.columns)
        if not columns:
            parts.append(
                f'<text x="{x + 14}" y="{y + header_height + 24}" font-family="Arial, sans-serif" font-size="12" fill="#64748b">No columns</text>'
            )
        else:
            for index, column in enumerate(columns):
                text_y = y + header_height + 22 + index * row_height
                label = escape(column_label(column))
                font_weight = "700" if column.primary_key else "400"
                fill = "#0f172a" if column.primary_key else "#334155"
                parts.append(
                    f'<text x="{x + 14}" y="{text_y}" font-family="Arial, sans-serif" font-size="12" font-weight="{font_weight}" fill="{fill}">{label}</text>'
                )

    parts.append("</svg>")
    output.write_text("\n".join(parts), encoding="utf-8")
    return output
