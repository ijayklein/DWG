#!/usr/bin/env bash
# Mark JSON entity locations for the project sample drawing example.dwg + elements.json
#
# ezdxf cannot read binary .dwg without ODA File Converter. Two workflows:
#
# A) One-time: export example.dwg → example.dxf (AutoCAD / BricsCAD: SAVEAS DXF).
#    Then this script merges markers into the DXF.
#
# B) No DXF: writes example_entity_markers.dxf (markers only, same WCS as the JSON).
#    In AutoCAD: open example.dwg, then INSERT or XREF example_entity_markers.dxf at 0,0,0.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
SCRIPT="${ROOT}/mark_json_entities_on_dwg.py"
JSON="${ROOT}/elements.json"
OUT_DXF="${ROOT}/example_marked.dxf"
OUT_MARKERS="${ROOT}/example_entity_markers.dxf"

if [[ ! -x "$PY" ]]; then
  echo "Create venv first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
if [[ ! -f "$JSON" ]]; then
  echo "Missing $JSON" >&2
  exit 1
fi

if [[ -f "${ROOT}/example.dxf" ]]; then
  echo "example.dxf found — summary (before marking):"
  "$PY" "${ROOT}/show_dxf_before_mark.py" "${ROOT}/example.dxf" || true
  echo ""
  echo "Merging markers into example.dxf → ${OUT_DXF}"
  exec "$PY" "$SCRIPT" --dwg "${ROOT}/example.dxf" --json "$JSON" --output "$OUT_DXF"
fi

echo "No example.dxf found. Binary example.dwg cannot be read by ezdxf without ODA File Converter."
echo "Writing markers-only DXF → ${OUT_MARKERS} (overlay on example.dwg at origin)."
exec "$PY" "$SCRIPT" --markers-only --json "$JSON" --output "$OUT_MARKERS"
