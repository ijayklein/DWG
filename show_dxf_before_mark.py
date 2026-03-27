#!/usr/bin/env python3
"""
Print a concise summary of a DXF (layers, modelspace counts, extents).
Use on example.dxf *before* running mark_json_entities_on_dwg / mark_example_entities.sh.

Cannot create DXF from binary .dwg — export example.dwg → example.dxf in CAD first.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import ezdxf


def main() -> int:
    ap = argparse.ArgumentParser(description="Summarize a DXF before JSON marking.")
    ap.add_argument("dxf", type=Path, help="Path to .dxf (e.g. example.dxf)")
    args = ap.parse_args()
    path = args.dxf.expanduser().resolve()
    if not path.is_file():
        print(f"Not found: {path}", file=sys.stderr)
        print(
            "\nCreate it from example.dwg in AutoCAD/BricsCAD: SAVEAS → "
            "Drawing Exchange (.dxf), same folder as example.dwg.",
            file=sys.stderr,
        )
        return 1

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()

    types = Counter(e.dxftype() for e in msp)
    layers = sorted(doc.layers.keys(), key=str.casefold)

    ext = doc.extents()
    if ext.has_data:
        lo, hi = ext.extmin, ext.extmax
        ext_str = f"extents min=({lo.x:.3f},{lo.y:.3f},{lo.z:.3f}) max=({hi.x:.3f},{hi.y:.3f},{hi.z:.3f})"
    else:
        ext_str = "extents (empty — no measurable geometry in drawing)"

    print(f"File: {path} ({path.stat().st_size:,} bytes)")
    print(f"DXF version: {doc.dxfversion}")
    print(ext_str)
    print(f"Layers ({len(layers)}): {', '.join(layers[:30])}{' …' if len(layers) > 30 else ''}")
    print(f"Modelspace entities: {len(msp)} total")
    for name, n in types.most_common(25):
        print(f"  {name}: {n}")
    if len(types) > 25:
        print(f"  … +{len(types) - 25} more types")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
