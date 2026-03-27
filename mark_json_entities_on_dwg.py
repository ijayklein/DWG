#!/usr/bin/env python3
"""
Overlay one marker per JSON entity onto a DWG/DXF so dense areas show where entities cluster.

Reads JSON with either:

- ``{ "autocad_document": { "entities": [ ... ] } }`` (e.g. ``elements.json``), or
- ``{ "entities": [ ... ] }`` (top-level list, e.g. some exports / Drive bundles).

Adds small circles (visible "dots") on a dedicated layer.

For the sample drawing **example.dwg** + **elements.json**, run ``./mark_example_entities.sh``
(needs **example.dxf** beside it for in-file merge, or get **example_entity_markers.dxf** for overlay).

Default: one representative point per entity (insertion / midpoint / centroid).
Optional: ``--mode vertices`` adds a dot at every vertex for polylines, hatches, and lines.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import ezdxf
from ezdxf import colors
from ezdxf.document import Drawing

LOG = logging.getLogger("mark_json_entities")


def open_drawing(path: Path) -> Drawing:
    """Open .dxf with ezdxf; open .dwg via ODA File Converter add-on if installed."""
    suf = path.suffix.lower()
    if suf == ".dxf":
        return ezdxf.readfile(path)
    if suf == ".dwg":
        from ezdxf.addons import odafc

        if not odafc.is_installed():
            raise OSError(
                "Cannot read binary .dwg without ODA File Converter.\n"
                "  • Install: https://www.opendesign.com/guestfiles/oda_file_converter\n"
                "  • Or export your drawing as .dxf from AutoCAD and pass that path.\n"
                "  • Or run with --markers-only to write a .dxf of markers only (overlay in CAD)."
            )
        return odafc.readfile(path)
    return ezdxf.readfile(path)


def new_markers_drawing() -> Drawing:
    """Minimal DXF for marker-only output (overlay / XREF at origin)."""
    doc = ezdxf.new("R2010")
    doc.header["$INSBASE"] = (0.0, 0.0, 0.0)
    return doc


def _parse_hex_color(s: str | None) -> int | None:
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"^#?([0-9a-fA-F]{6})$", s.strip())
    if not m:
        return None
    h = m.group(1)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return colors.rgb2int((r, g, b))


def _centroid_2d(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not points:
        return None
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    n = len(points)
    return sx / n, sy / n


def _get_xyz(d: dict[str, Any], key: str) -> tuple[float, float, float] | None:
    p = d.get(key)
    if not isinstance(p, dict):
        return None
    try:
        return float(p["x"]), float(p["y"]), float(p.get("z", 0.0))
    except (KeyError, TypeError, ValueError):
        return None


def _vertices_from_hatch_loops(loops: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    if not isinstance(loops, list):
        return out
    for loop in loops:
        if not isinstance(loop, dict):
            continue
        pl = loop.get("polyline")
        if not isinstance(pl, list):
            continue
        for seg in pl:
            if not isinstance(seg, dict):
                continue
            v = seg.get("vertex")
            if isinstance(v, dict):
                try:
                    out.append((float(v["x"]), float(v["y"])))
                except (KeyError, TypeError, ValueError):
                    pass
    return out


def representative_points(entity: dict[str, Any], *, mode: str) -> list[tuple[float, float, float]]:
    """Return WCS points to mark for this entity (mode: centroid | vertices)."""
    et = (entity.get("type") or "").lower()

    if mode == "vertices":
        if et == "line":
            a = _get_xyz(entity, "start_point")
            b = _get_xyz(entity, "end_point")
            pts = [p for p in (a, b) if p is not None]
            return pts
        if et == "polyline":
            verts = entity.get("vertices")
            if isinstance(verts, list):
                out: list[tuple[float, float, float]] = []
                for v in verts:
                    if isinstance(v, dict):
                        try:
                            out.append((float(v["x"]), float(v["y"]), 0.0))
                        except (KeyError, TypeError, ValueError):
                            pass
                return out
        if et == "hatch":
            v2 = _vertices_from_hatch_loops(entity.get("loops"))
            return [(x, y, 0.0) for x, y in v2]

    # centroid mode (default) or fallback for types above in centroid path
    if et in ("mtext", "text", "block_reference"):
        p = _get_xyz(entity, "position")
        return [p] if p else []

    if et == "line":
        a = _get_xyz(entity, "start_point")
        b = _get_xyz(entity, "end_point")
        if a and b:
            return [
                (
                    (a[0] + b[0]) / 2.0,
                    (a[1] + b[1]) / 2.0,
                    (a[2] + b[2]) / 2.0,
                )
            ]
        return [p for p in (a, b) if p]

    if et == "polyline":
        verts = entity.get("vertices")
        if isinstance(verts, list):
            pts2: list[tuple[float, float]] = []
            for v in verts:
                if isinstance(v, dict):
                    try:
                        pts2.append((float(v["x"]), float(v["y"])))
                    except (KeyError, TypeError, ValueError):
                        pass
            c = _centroid_2d(pts2)
            if c:
                return [(c[0], c[1], 0.0)]
        return []

    if et == "hatch":
        pts2 = _vertices_from_hatch_loops(entity.get("loops"))
        c = _centroid_2d(pts2)
        if c:
            return [(c[0], c[1], 0.0)]
        return []

    # Generic: insertion-like keys
    for key in ("position", "insertion_point", "center"):
        p = _get_xyz(entity, key)
        if p:
            return [p]
    return []


def load_entities(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")

    # Shape A: { "autocad_document": { "entities": [ ... ] } }  (e.g. elements.json)
    doc = data.get("autocad_document")
    if isinstance(doc, dict):
        entities = doc.get("entities")
        if isinstance(entities, list):
            return [e for e in entities if isinstance(e, dict)]

    # Shape B: { "entities": [ ... ] }  (e.g. some export / Drive bundles)
    root_entities = data.get("entities")
    if isinstance(root_entities, list):
        return [e for e in root_entities if isinstance(e, dict)]

    raise ValueError(
        "JSON must have either 'autocad_document.entities' or top-level 'entities' (list)."
    )


def ensure_layer(doc: ezdxf.EzDxf, name: str, *, color: int = colors.RED) -> None:
    if name not in doc.layers:
        doc.layers.add(name, color=color)


def add_markers(
    doc: ezdxf.EzDxf,
    points: Iterable[tuple[float, float, float, dict[str, Any]]],
    *,
    layer: str,
    radius: float,
    default_true_color: int,
    use_json_color: bool,
) -> int:
    msp = doc.modelspace()
    n = 0
    for x, y, z, meta in points:
        tc = default_true_color
        if use_json_color:
            parsed = _parse_hex_color(meta.get("color"))
            if parsed is not None:
                tc = parsed
        msp.add_circle(
            (x, y, z),
            radius,
            dxfattribs={
                "layer": layer,
                "true_color": tc,
            },
        )
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Draw JSON entity locations as dots on a DWG/DXF.")
    ap.add_argument(
        "--dwg",
        type=Path,
        default=None,
        help="Input .dwg or .dxf to merge markers into (not required with --markers-only)",
    )
    ap.add_argument("--json", type=Path, required=True, help="elements.json (autocad_document.entities)")
    ap.add_argument("--output", type=Path, required=True, help="Output .dwg or .dxf path")
    ap.add_argument(
        "--markers-only",
        action="store_true",
        help="Ignore base drawing; write a DXF containing only markers (same WCS coords) for overlay",
    )
    ap.add_argument(
        "--mode",
        choices=("centroid", "vertices"),
        default="centroid",
        help="centroid: one dot per entity; vertices: dots at polyline/hatch/line vertices",
    )
    ap.add_argument("--layer", default="JSON_ENTITY_MARKERS", help="Layer name for markers")
    ap.add_argument(
        "--radius",
        type=float,
        default=0.0,
        help="Circle radius in drawing units; 0 = auto from extent (max 50, min 0.5)",
    )
    ap.add_argument(
        "--use-json-color",
        action="store_true",
        help="Use each entity's #RRGGBB color when valid (else red)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    if not args.verbose:
        logging.getLogger("ezdxf").setLevel(logging.WARNING)

    entities = load_entities(args.json)
    LOG.info("Loaded %d entities from %s", len(entities), args.json)

    if args.markers_only:
        doc = new_markers_drawing()
    else:
        if not args.dwg:
            LOG.error("Provide --dwg or use --markers-only.")
            return 2
        doc = open_drawing(args.dwg)
    default_tc = colors.rgb2int((255, 0, 0))

    # Collect all candidate coordinates for auto radius
    all_xy: list[tuple[float, float]] = []
    staged: list[tuple[float, float, float, dict[str, Any]]] = []
    skipped = 0

    for ent in entities:
        pts = representative_points(ent, mode=args.mode)
        if not pts:
            skipped += 1
            continue
        for x, y, z in pts:
            all_xy.append((x, y))
            staged.append((x, y, z, ent))

    radius = args.radius
    if radius <= 0 and len(all_xy) >= 2:
        xs = [p[0] for p in all_xy]
        ys = [p[1] for p in all_xy]
        extent = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
        radius = min(50.0, max(0.5, extent * 0.001))
    elif radius <= 0:
        radius = 1.0

    LOG.info("Marker radius: %.4f", radius)

    ensure_layer(doc, args.layer, color=colors.RED)
    n = add_markers(
        doc,
        staged,
        layer=args.layer,
        radius=radius,
        default_true_color=default_tc,
        use_json_color=args.use_json_color,
    )
    LOG.info("Added %d markers on layer %s (%d entities had no drawable point)", n, args.layer, skipped)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(args.output)
    LOG.info("Wrote %s", args.output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        LOG.error("%s", e)
        sys.exit(1)
