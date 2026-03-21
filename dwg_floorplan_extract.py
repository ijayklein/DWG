#!/usr/bin/env python3
"""
CAD floor-plan → PDF extractor per PRP DWG.md.
Uses ezdxf for parsing/clustering; QCAD dwg2pdf for vector rendering.

QCAD's PDF exporter does not create PDF Optional Content Groups (true PDF "layers"):
see forum.qcad.org (Andrew: Qt stack lacks OCG). Single dwg2pdf runs therefore look
"flat" in Acrobat's Layers panel. Use --pdf-layers to merge per-layer dwg2pdf exports
with PyMuPDF so each CAD layer becomes a toggleable OCG (slower: one QCAD run per layer).

When plotting a *layout* with ``-layer=…``, QCAD still applies each viewport’s frozen/off
visibility. Many sheets only show dimensions in the model viewport for most layer
filters, so OCG merges can look like “only measurement lines”. For ``--pdf-layers`` on
layouts we therefore slice model space to the main layout viewport’s WCS window, export
that temporary DXF with ``-block=Model``, then merge OCGs (no per-viewport freeze in Model).

**DWG workflow:** By default, ``.dwg`` inputs are converted once to ``.dxf`` via ODA File
Converter (layer table preserved in the DXF). All plotting uses that DXF so QCAD Community
Edition (DXF-native) can be used without relying on Pro DWG readers. ODA’s converter has no
CLI switches for colors; use ``--pdf-black-lines`` to normalize linework to ACI black after DXF.
"""
from __future__ import annotations

import argparse
import logging
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import ezdxf
import numpy as np
from ezdxf import bbox
from ezdxf.addons import importer
from sklearn.cluster import DBSCAN

LOG = logging.getLogger("dwg_floorplan_extract")

DEFAULT_DWG2PDF = "/Applications/QCAD-Pro.app/Contents/Resources/dwg2pdf"
ALT_DWG2PDF = "/Applications/QCAD.app/Contents/Resources/dwg2pdf"
DEFAULT_ODA = "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"
DEFAULT_EXTS = (".dxf", ".dwg")


@dataclass(frozen=True)
class Region:
    """A region to export: optional label and axis-aligned bounds in drawing units."""

    label: str
    min_x: float
    min_y: float
    max_x: float
    max_y: float


def _expand_bbox(
    mn_x: float, mn_y: float, mx_x: float, mx_y: float, margin: float
) -> tuple[float, float, float, float]:
    w = mx_x - mn_x
    h = mx_y - mn_y
    if w <= 0 or h <= 0:
        return mn_x, mn_y, mx_x, mx_y
    dx = w * margin
    dy = h * margin
    return mn_x - dx, mn_y - dy, mx_x + dx, mx_y + dy


def _region_overlaps_bbox2d(reg: Region, eb: bbox.BoundingBox) -> bool:
    if not eb.has_data or eb.extmin is None or eb.extmax is None:
        return False
    b0, b1 = eb.extmin, eb.extmax
    return not (
        reg.max_x < float(b0.x)
        or reg.min_x > float(b1.x)
        or reg.max_y < float(b0.y)
        or reg.min_y > float(b1.y)
    )


def _intersects(a: bbox.BoundingBox, b: Region) -> bool:
    aa = a.extmin, a.extmax
    if aa[0] is None or aa[1] is None:
        return False
    ax0, ay0, _ = aa[0]
    ax1, ay1, _ = aa[1]
    return not (ax1 < b.min_x or ax0 > b.max_x or ay1 < b.min_y or ay0 > b.max_y)


def export_dwg_to_dxf_file(
    dwg: Path, dxf_out: Path, oda_exec: Path, out_version: str
) -> bool:
    """
    Convert DWG → DXF with ODA File Converter; writes ``dxf_out`` (layers preserved in DXF).

    ODA’s CLI only supports audit/recursive/version/path — it does **not** offer monochrome or
    color-normalization. For black linework use ``--pdf-black-lines`` (post-processes the DXF).
    """
    tmp = Path(tempfile.mkdtemp(prefix="floorplan_oda_"))
    try:
        args = [
            str(oda_exec),
            str(dwg.parent.resolve()),
            str(tmp.resolve()),
            out_version,
            "DXF",
            "0",
            "0",
            dwg.name,
        ]
        r = subprocess.run(args, capture_output=True, text=True, timeout=600)
        produced = tmp / dwg.with_suffix(".dxf").name
        if not produced.is_file():
            LOG.error(
                "ODA File Converter did not produce %s (exit %s). stderr: %s",
                produced.name,
                r.returncode,
                (r.stderr or "")[:800],
            )
            return False
        dxf_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(produced, dxf_out)
        return True
    except OSError as e:
        LOG.error("Could not run ODA File Converter (%s): %s", oda_exec, e)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _dwg_to_dxf_via_oda(dwg: Path, oda_exec: Path, out_version: str) -> ezdxf.Drawing | None:
    """Convert DWG to DXF in a temp dir and load with ezdxf (no persistent file)."""
    tmp = Path(tempfile.mkdtemp(prefix="floorplan_oda_"))
    try:
        args = [
            str(oda_exec),
            str(dwg.parent.resolve()),
            str(tmp.resolve()),
            out_version,
            "DXF",
            "0",
            "0",
            dwg.name,
        ]
        r = subprocess.run(args, capture_output=True, text=True, timeout=600)
        out = tmp / dwg.with_suffix(".dxf").name
        if not out.is_file():
            LOG.error(
                "ODA File Converter did not produce %s (exit %s). stderr: %s",
                out.name,
                r.returncode,
                (r.stderr or "")[:800],
            )
            return None
        doc = ezdxf.readfile(str(out))
        doc.filename = str(dwg)
        return doc
    except OSError as e:
        LOG.error("Could not run ODA File Converter (%s): %s", oda_exec, e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _entity_force_aci_black(entity: Any, aci: int = 7) -> None:
    """Force ACI pen color (7 = black/white on paper) and drop true-color / transparency."""
    try:
        dx = entity.dxf
    except Exception:
        return
    try:
        if not dx.hasattr("color"):
            return
    except Exception:
        return
    try:
        dx.color = aci
    except Exception:
        return
    for attr in ("true_color", "transparency"):
        try:
            if dx.hasattr(attr):
                dx.discard(attr)
        except Exception:
            pass
    try:
        if hasattr(entity, "rgb"):
            entity.rgb = None
    except Exception:
        pass


def force_pdf_black_lines(doc: ezdxf.Drawing) -> None:
    """
    Set every layer default and every graphic entity to ACI 7 (prints black on a light PDF
    background) and remove true-color overrides. Applied to Model, paper layouts, and blocks.
    """
    try:
        for layer in doc.layers:
            try:
                layer.dxf.color = 7
            except Exception:
                pass
    except Exception as e:
        LOG.debug("Layer table color reset: %s", e)

    try:
        for layout in doc.layouts:
            for entity in layout:
                _entity_force_aci_black(entity, 7)
    except Exception as e:
        LOG.warning("Could not walk all layouts for black lines: %s", e)

    try:
        for block in doc.blocks:
            name = getattr(block, "name", "") or ""
            if name.startswith("*"):
                continue
            for entity in block:
                _entity_force_aci_black(entity, 7)
    except Exception as e:
        LOG.warning("Could not walk all blocks for black lines: %s", e)


def black_lines_plot_path(src: Path, work_dir: Path) -> Path | None:
    """
    Write ``{stem}_black.dxf`` with all linework forced to ACI black for QCAD PDF export.
    Reuses the file if newer than ``src``.
    """
    if src.suffix.lower() != ".dxf":
        LOG.error(
            "--pdf-black-lines requires a DXF workflow (default DWG preflight). "
            "Do not use --no-dwg-preflight with .dwg, or convert to .dxf first."
        )
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / f"{src.stem}_black{src.suffix}"
    if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
        LOG.info("Reusing black-line DXF (up to date): %s", out)
        return out.resolve()
    try:
        doc = ezdxf.readfile(str(src))
    except Exception as e:
        LOG.error("Could not read %s for --pdf-black-lines: %s", src, e)
        return None
    force_pdf_black_lines(doc)
    try:
        doc.saveas(str(out))
    except Exception as e:
        LOG.error("Could not write %s: %s", out, e)
        return None
    LOG.info("Wrote monochrome plot DXF (ACI 7 linework): %s", out)
    return out.resolve()


def resolve_plot_dxf(
    source: Path,
    dxf_work_dir: Path,
    oda_exec: Path | None,
    out_version: str,
    *,
    preflight: bool,
    force_convert: bool,
) -> Path | None:
    """
    If ``source`` is ``.dwg`` and ``preflight``, return path to converted ``{stem}.dxf``.
    Otherwise return ``source``. Missing ODA when preflight is required returns ``None``.
    """
    if source.suffix.lower() != ".dwg":
        return source
    if not preflight:
        return source
    if not oda_exec or not oda_exec.is_file():
        LOG.error(
            "DWG → DXF preflight needs ODA File Converter (see --oda-converter or install ODA)."
        )
        return None
    dxf_work_dir.mkdir(parents=True, exist_ok=True)
    dxf_out = dxf_work_dir / f"{source.stem}.dxf"
    if (
        dxf_out.is_file()
        and not force_convert
        and dxf_out.stat().st_mtime >= source.stat().st_mtime
    ):
        LOG.info("Reusing DXF newer than DWG: %s", dxf_out)
        return dxf_out.resolve()
    if not export_dwg_to_dxf_file(source, dxf_out, oda_exec, out_version):
        return None
    LOG.info("DWG → DXF (layers in DXF): %s", dxf_out)
    return dxf_out.resolve()


def load_drawing(
    path: Path,
    oda_converter: Path | None = None,
    dxf_out_version: str = "ACAD2013",
) -> ezdxf.Drawing | None:
    if path.suffix.lower() == ".dwg":
        oda = oda_converter or Path(DEFAULT_ODA)
        if not oda.is_file():
            LOG.error(
                "Reading .dwg requires ODA File Converter. Install from Open Design Alliance "
                "or pass --oda-converter. Expected at %s",
                oda,
            )
            return None
        return _dwg_to_dxf_via_oda(path, oda, dxf_out_version)

    try:
        return ezdxf.readfile(str(path))
    except ezdxf.DXFStructureError as e:
        LOG.error("DXF structure error in %s: %s", path, e)
        return None
    except IOError as e:
        LOG.error("Cannot read %s: %s", path, e)
        return None


def strategy_layouts(doc: ezdxf.Drawing) -> list[Region]:
    out: list[Region] = []
    for name in doc.layouts.names():
        if name.lower() == "model":
            continue
        out.append(Region(label=name, min_x=0, min_y=0, max_x=0, max_y=0))
    return out


def strategy_blocks(
    doc: ezdxf.Drawing, block_patterns: list[re.Pattern[str]]
) -> list[Region]:
    msp = doc.modelspace()
    regions: list[Region] = []
    for e in msp.query("INSERT"):
        name = e.dxf.name
        if not any(p.search(name) for p in block_patterns):
            continue
        try:
            cbox = e.calc_geometry_extents()
        except Exception:
            continue
        if cbox is None:
            continue
        mn, mx = cbox
        regions.append(
            Region(
                label=name,
                min_x=float(mn.x),
                min_y=float(mn.y),
                max_x=float(mx.x),
                max_y=float(mx.y),
            )
        )
    return regions


def _iter_sample_points(msp, max_entities: int) -> Iterator[tuple[float, float]]:
    for n, e in enumerate(msp):
        if n >= max_entities:
            break
        try:
            c = bbox.extents([e])
        except Exception:
            continue
        if c.has_data:
            mn, mx = c.extmin, c.extmax
            cx = (float(mn.x) + float(mx.x)) / 2
            cy = (float(mn.y) + float(mx.y)) / 2
            yield cx, cy


def strategy_cluster(
    doc: ezdxf.Drawing,
    eps: float,
    min_samples: int,
    margin: float,
    max_entities: int,
) -> list[Region]:
    msp = doc.modelspace()
    pts = list(_iter_sample_points(msp, max_entities))
    if len(pts) < min_samples:
        LOG.warning("Not enough geometry samples for clustering (%s)", len(pts))
        return []

    X = np.array(pts, dtype=float)
    labels = DBSCAN(eps=eps, min_samples=min_samples).fit_predict(X)
    regions: list[Region] = []
    for k in sorted(set(labels)):
        if k == -1:
            continue
        mk = X[labels == k]
        mn_x, mn_y = float(mk[:, 0].min()), float(mk[:, 1].min())
        mx_x, mx_y = float(mk[:, 0].max()), float(mk[:, 1].max())
        mn_x, mn_y, mx_x, mx_y = _expand_bbox(mn_x, mn_y, mx_x, mx_y, margin)
        regions.append(
            Region(label=f"cluster_{k}", min_x=mn_x, min_y=mn_y, max_x=mx_x, max_y=mx_y)
        )
    return regions


def copy_intersecting_to_new_doc(
    src: ezdxf.Drawing, region: Region, max_bbox_area: float | None
) -> ezdxf.Drawing | None:
    if region.max_x > region.min_x and region.max_y > region.min_y:
        area = (region.max_x - region.min_x) * (region.max_y - region.min_y)
        if max_bbox_area is not None and area > max_bbox_area:
            LOG.warning("Skipping %s: bbox area %.4g exceeds max", region.label, area)
            return None

    dst = ezdxf.new(src.dxfversion)
    dst.units = src.units
    imp = importer.Importer(src, dst)
    msp = src.modelspace()
    to_import = []
    nonempty = False
    for e in msp:
        try:
            cb = bbox.extents([e])
        except Exception:
            continue
        if not cb.has_data:
            continue
        if region.max_x > region.min_x and region.max_y > region.min_y:
            if not _intersects(cb, region):
                continue
        to_import.append(e)
        nonempty = True
    if not nonempty:
        LOG.warning("No geometry for region %s", region.label)
        return None
    imp.import_entities(to_import)
    imp.finalize()
    return dst


def resolve_dwg2pdf(user_path: str | None) -> Path | None:
    candidates = []
    if user_path:
        candidates.append(Path(user_path))
    candidates.append(Path(DEFAULT_DWG2PDF))
    candidates.append(Path(ALT_DWG2PDF))
    for p in candidates:
        if p.is_file() and os_access_x(p):
            return p
    return None


def os_access_x(p: Path) -> bool:
    import os

    return os.access(p, os.X_OK)


def run_dwg2pdf(
    exe: Path,
    infile: Path,
    outfile: Path,
    block: str | None,
    extra: list[str],
    layer_regex: str | None = None,
) -> bool:
    cmd: list[str] = [str(exe)]
    # QCAD macOS builds ship cocoa, not offscreen; Linux headless may use offscreen.
    if platform.system() == "Darwin":
        cmd.extend(["-platform", "cocoa"])
    else:
        cmd.extend(["-platform", "offscreen"])
    cmd.extend(["-f", "-a", "-o", str(outfile)])
    if block:
        cmd.append(f"-block={block}")
    cmd.extend(extra)
    if layer_regex is not None:
        cmd.append(f"-layer={layer_regex}")
    cmd.append(str(infile))
    LOG.info("Running: %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except OSError as e:
        LOG.error("Failed to execute dwg2pdf (%s): %s", exe, e)
        _gatekeeper_hint(str(e))
        return False
    if r.returncode != 0:
        LOG.error("dwg2pdf failed (%s): %s", r.returncode, (r.stderr or r.stdout)[:4000])
        combined = (r.stderr or "") + (r.stdout or "")
        if "Operation not permitted" in combined or "denied" in combined.lower():
            _gatekeeper_hint(combined)
        return False
    if not outfile.is_file():
        LOG.error("Expected output missing: %s", outfile)
        return False
    return True


def _gatekeeper_hint(msg: str) -> None:
    if "not permitted" in msg.lower() or "gatekeeper" in msg.lower():
        LOG.error(
            "macOS may be blocking QCAD. Allow the app in "
            "System Settings → Privacy & Security, or run: "
            "xattr -dr com.apple.quarantine /Applications/QCAD.app"
        )
    if "Incompatible processor" in msg or "neon" in msg.lower():
        LOG.error(
            "This QCAD build cannot run on this CPU / environment. Install a compatible QCAD "
            "build or run on the machine where QCAD is supported."
        )


def sanitize_label(s: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", s)[:120] or "plan"


def primary_viewport_model_region(
    doc: ezdxf.Drawing, layout_name: str, margin: float = 0.02
) -> Region | None:
    """
    Bounding box in WCS for the largest visible layout viewport’s model-space view.
    Used to build a Model DXF slice for OCG export (avoids layout viewport layer masking).
    """
    try:
        layout = doc.layouts.get(layout_name)
    except Exception:
        return None
    best: tuple[float, Any] | None = None
    for ent in layout:
        if ent.dxftype() != "VIEWPORT":
            continue
        if not ent.is_visible:
            continue
        try:
            w, h = float(ent.dxf.width), float(ent.dxf.height)
        except Exception:
            continue
        if w <= 1e-6 or h <= 1e-6:
            continue
        area = w * h
        if best is None or area > best[0]:
            best = (area, ent)
    if best is None:
        return None
    vp = best[1]
    try:
        mn_x, mn_y, mx_x, mx_y = vp.get_modelspace_limits()
    except Exception as e:
        LOG.debug("Viewport model limits failed for %s: %s", layout_name, e)
        return None
    mn_x, mn_y, mx_x, mx_y = _expand_bbox(mn_x, mn_y, mx_x, mx_y, margin)
    return Region(
        label=f"{layout_name}_viewport", min_x=mn_x, min_y=mn_y, max_x=mx_x, max_y=mx_y
    )


def model_extent_wcs(doc: ezdxf.Drawing) -> tuple[float, float, float, float] | None:
    ext = bbox.extents(doc.modelspace())
    if not ext.has_data or ext.extmin is None or ext.extmax is None:
        return None
    return (
        float(ext.extmin.x),
        float(ext.extmin.y),
        float(ext.extmax.x),
        float(ext.extmax.y),
    )


def pdf_clip_for_wcs_region(
    region: Region,
    mminx: float,
    mminy: float,
    mmaxx: float,
    mmaxy: float,
    page_rect,
):
    """
    Map axis-aligned WCS rectangle to PyMuPDF clip on a reference page (y-down PDF coords).
    Assumes QCAD plotted Model with -a uniform scale from the given WCS extent to page_rect.
    """
    import fitz

    dw = max(mmaxx - mminx, 1e-9)
    dh = max(mmaxy - mminy, 1e-9)
    pw, ph = float(page_rect.width), float(page_rect.height)
    scale = min(pw / dw, ph / dh)
    cw, ch = dw * scale, dh * scale
    ox = (pw - cw) / 2.0
    oy = (ph - ch) / 2.0

    def wx_to_px(wx: float) -> float:
        return ox + (wx - mminx) * scale

    def wy_to_py(wy: float) -> float:
        return oy + (mmaxy - wy) * scale

    left = wx_to_px(region.min_x)
    right = wx_to_px(region.max_x)
    if left > right:
        left, right = right, left
    top = wy_to_py(region.max_y)
    bottom = wy_to_py(region.min_y)
    if top > bottom:
        top, bottom = bottom, top
    return fitz.Rect(left, top, right, bottom)


def crop_pdf_page_to_rect(src_pdf: Path, clip, out_pdf: Path) -> bool:
    """Copy page 0 of ``src_pdf`` through ``clip`` into a single-page PDF sized to the clip."""
    try:
        import fitz
    except ImportError:
        LOG.error("crop_pdf_page_to_rect requires PyMuPDF: pip install pymupdf")
        return False
    w, h = float(clip.width), float(clip.height)
    if w < 2 or h < 2:
        return False
    src = fitz.open(str(src_pdf))
    try:
        if src.page_count < 1:
            return False
        dst = fitz.open()
        try:
            page = dst.new_page(width=w, height=h)
            dest = fitz.Rect(0, 0, w, h)
            page.show_pdf_page(dest, src, 0, clip=clip)
            dst.save(str(out_pdf))
        finally:
            dst.close()
    finally:
        src.close()
    return True


def merge_layer_pdfs_cropped_ocg(parts: list[tuple[str, Path]], clip, out_pdf: Path) -> bool:
    """Same page size per layer; crop each to ``clip`` in source space; stack as OCGs."""
    try:
        import fitz
    except ImportError:
        LOG.error("PyMuPDF required: pip install pymupdf")
        return False
    nonempty = [(n, p) for n, p in parts if p.is_file() and p.stat().st_size > 80]
    if not nonempty:
        return False
    nonempty = sorted(nonempty, key=lambda x: x[1].stat().st_size, reverse=True)
    w, h = float(clip.width), float(clip.height)
    if w < 2 or h < 2:
        return False
    merged = fitz.open()
    try:
        page = merged.new_page(width=w, height=h)
        dest = fitz.Rect(0, 0, w, h)
        for name, ppath in nonempty:
            ocg_xref = merged.add_ocg(name)
            src = fitz.open(str(ppath))
            try:
                if src.page_count < 1:
                    continue
                page.show_pdf_page(dest, src, 0, clip=clip, oc=ocg_xref, overlay=True)
            finally:
                src.close()
        merged.save(str(out_pdf))
    finally:
        merged.close()
    return True


def cad_layer_names(
    doc: ezdxf.Drawing,
    skip: frozenset[str] = frozenset({"DEFPOINTS"}),
) -> list[str]:
    skip_u = {x.upper() for x in skip}
    out: list[str] = []
    try:
        for layer in doc.layers:
            n = layer.dxf.name
            if n in skip or n.upper() in skip_u:
                continue
            out.append(n)
    except Exception as e:
        LOG.warning("Could not iterate layer table: %s", e)
    return out


def layer_names_capped(names: list[str], max_pdf_layers: int) -> list[str]:
    """``max_pdf_layers <= 0`` keeps the full list (every CAD layer)."""
    if max_pdf_layers <= 0:
        return names
    return names[:max_pdf_layers]


def layers_referenced_in_modelspace(doc: ezdxf.Drawing) -> set[str]:
    """
    Layers that actually carry geometry visible from *Model*: direct modelspace entities
    plus entities nested in inserted blocks (recursive). QCAD ``-layer=^name$`` only shows
    entities on that layer; table entries with nothing in Model produce blank PDFs.
    """
    out: set[str] = set()
    visited_blocks: set[str] = set()

    def walk_block(name: str, depth: int = 0) -> None:
        if depth > 64 or name in visited_blocks:
            return
        visited_blocks.add(name)
        try:
            blk = doc.blocks[name]
        except KeyError:
            return
        for e in blk:
            try:
                out.add(e.dxf.layer)
            except Exception:
                pass
            if e.dxftype() == "INSERT":
                walk_block(e.dxf.name, depth + 1)

    msp = doc.modelspace()
    for e in msp:
        try:
            out.add(e.dxf.layer)
        except Exception:
            pass
        if e.dxftype() == "INSERT":
            walk_block(e.dxf.name)
    return out


def model_canvas_layer_names(
    doc: ezdxf.Drawing,
    max_pdf_layers: int,
    *,
    all_table_layers: bool,
) -> list[str]:
    base = layer_names_capped(cad_layer_names(doc), max_pdf_layers)
    if all_table_layers:
        return base
    used = layers_referenced_in_modelspace(doc)
    filtered = [n for n in base if n in used]
    nskip = len(base) - len(filtered)
    if nskip:
        LOG.info(
            "model-canvas: omitting %s unused layer table entr(y/ies) (no Model geometry on that "
            "layer, including inside blocks). Pass --model-canvas-all-table-layers to export them anyway.",
            nskip,
        )
    return filtered


def pdf_page_looks_blank(path: Path) -> bool:
    """Heuristic: QCAD can emit a valid but empty-looking single-page vector PDF."""
    try:
        import fitz
    except ImportError:
        return False
    try:
        doc = fitz.open(str(path))
        try:
            if doc.page_count < 1:
                return True
            page = doc[0]
            if page.get_drawings():
                return False
            if page.get_images():
                return False
            txt = (page.get_text("text") or "").strip()
            return len(txt) <= 12
        finally:
            doc.close()
    except Exception:
        return False


def merge_pdfs_as_ocg(layer_parts: list[tuple[str, Path]], out_pdf: Path) -> bool:
    """Stack single-page PDFs on one page; each stack level is one PDF OCG (Acrobat \"layer\")."""
    try:
        import fitz
    except ImportError:
        LOG.error("--pdf-layers requires PyMuPDF: pip install pymupdf")
        return False

    nonempty = [(n, p) for n, p in layer_parts if p.is_file() and p.stat().st_size > 80]
    if not nonempty:
        return False
    # Draw larger exports first (model linework), smaller last (dims/annotation on top).
    nonempty = sorted(nonempty, key=lambda x: x[1].stat().st_size, reverse=True)

    ref = fitz.open(str(nonempty[0][1]))
    try:
        if ref.page_count < 1:
            return False
        rect = ref[0].rect
    finally:
        ref.close()

    merged = fitz.open()
    try:
        page = merged.new_page(width=rect.width, height=rect.height)
        for name, ppath in nonempty:
            ocg_xref = merged.add_ocg(name)
            src = fitz.open(str(ppath))
            try:
                if src.page_count < 1:
                    continue
                r1 = src[0].rect
                if abs(r1.width - rect.width) > 1.0 or abs(r1.height - rect.height) > 1.0:
                    LOG.debug("Layer %r page size %s differs from reference %s", name, r1, rect)
                page.show_pdf_page(rect, src, 0, oc=ocg_xref, overlay=True)
            finally:
                src.close()
        merged.save(str(out_pdf))
    finally:
        merged.close()
    return True


def export_plan_pdf_with_ocg_layers(
    dwg2pdf: Path,
    qcad_input: Path,
    block: str | None,
    layer_names: list[str],
    out_pdf: Path,
    extra_args: list[str],
    workdir: Path,
    min_layer_bytes: int = 120,
) -> bool:
    """
    One dwg2pdf run per CAD layer (vector), then merge into one PDF with OCGs.
    """
    parts: list[tuple[str, Path]] = []
    for idx, lyr in enumerate(layer_names):
        pat = f"^{re.escape(lyr)}$"
        layer_pdf = workdir / f"_lyr_{idx:04d}_{sanitize_label(lyr)}.pdf"
        if not run_dwg2pdf(
            dwg2pdf, qcad_input, layer_pdf, block, extra_args, layer_regex=pat
        ):
            LOG.debug("dwg2pdf skipped or failed for layer %r", lyr)
            continue
        if layer_pdf.stat().st_size < min_layer_bytes:
            LOG.debug("Omitting near-empty export for layer %r", lyr)
            continue
        parts.append((lyr, layer_pdf))

    if not parts:
        LOG.error("No per-layer PDFs were produced; check -layer patterns and drawing paths.")
        return False
    if len(parts) == 1:
        shutil.copy(parts[0][1], out_pdf)
        LOG.warning(
            "Only one CAD layer produced a usable PDF; copied flat (add geometry to other layers)."
        )
        return True
    if not merge_pdfs_as_ocg(parts, out_pdf):
        LOG.error("PyMuPDF OCG merge failed.")
        return False
    LOG.info("Merged %s CAD layer(s) into OCG PDF: %s", len(parts), out_pdf)
    _log_ocg_count(out_pdf)
    return True


def _log_ocg_count(pdf_path: Path) -> None:
    try:
        import fitz
    except ImportError:
        return
    doc = fitz.open(str(pdf_path))
    try:
        oc = doc.get_ocgs()
        n = len(oc) if oc else 0
        if n:
            LOG.info("Verified PDF optional content groups: %s (open in Adobe Acrobat → Layers).", n)
        else:
            LOG.warning(
                "%s: no OCG entries found after merge; PDF may look flat in viewers.",
                pdf_path,
            )
    finally:
        doc.close()


def _process_model_canvas(
    path: Path,
    out_dir: Path,
    dwg2pdf: Path,
    regions: list[Region],
    extra_args: list[str],
    source_doc: ezdxf.Drawing | None,
    max_pdf_layers: int,
    stem: str,
    model_canvas_all_table_layers: bool,
) -> int:
    """
    Multi-layer Model workflow:

    1. Export one **merged** Model PDF (all layers) — same “canvas” you use to see every
       floor plan; WCS→PDF mapping for crops comes from this page.
    2. **Regions** are fixed from prior detection (``strategy_cluster`` on full modelspace —
       all layers’ geometry). One region per intended floor plan; tune DBSCAN if you get too
       few/many.
    3. For **each CAD layer**, export a **full** Model PDF (one canvas = one layer), then
       **crop** that PDF with each region’s clip → ``*_Layer*_full.pdf`` + ``*_Layer*_Plan*_*.pdf``.
    4. For each plan, merge that plan’s per-layer crops into one **OCG** PDF (Acrobat layers).
    """
    if source_doc is None:
        LOG.error("model-canvas requires a loaded drawing.")
        return 0
    if not regions:
        LOG.error("model-canvas: no regions (tune --cluster-eps / --cluster-min-samples).")
        return 0
    try:
        import fitz
    except ImportError:
        LOG.error("model-canvas requires PyMuPDF: pip install pymupdf")
        return 0
    mex = model_extent_wcs(source_doc)
    if mex is None:
        LOG.error("model-canvas: could not get model space extent.")
        return 0
    mminx, mminy, mmaxx, mmaxy = mex

    ref_out = out_dir / f"{stem}_Model_merged_reference.pdf"
    LOG.info("model-canvas phase 1: merged multi-layer Model canvas → %s", ref_out)
    if not run_dwg2pdf(dwg2pdf, path, ref_out, "Model", extra_args):
        return 0

    rdoc = fitz.open(str(ref_out))
    try:
        if rdoc.page_count < 1:
            LOG.error("Reference PDF has no pages.")
            return 0
        page_rect = rdoc[0].rect
    finally:
        rdoc.close()

    clips: list[tuple[Region, Any]] = []
    for reg in regions:
        clip = pdf_clip_for_wcs_region(reg, mminx, mminy, mmaxx, mmaxy, page_rect)
        clip = clip.intersect(page_rect)
        if clip.is_empty or clip.width < 2 or clip.height < 2:
            LOG.warning("model-canvas: skip region %s — empty or invalid PDF clip.", reg.label)
            continue
        clips.append((reg, clip))
    if not clips:
        LOG.error("model-canvas: no valid regions after clipping.")
        return 0
    LOG.info(
        "model-canvas: %s floor-plan region(s) aligned to merged canvas; next: one full PDF per CAD layer, then crop per region.",
        len(clips),
    )

    names = model_canvas_layer_names(
        source_doc,
        max_pdf_layers,
        all_table_layers=model_canvas_all_table_layers,
    )
    if not names:
        LOG.error(
            "model-canvas: no layers to export after filtering; try --model-canvas-all-table-layers."
        )
        return 0
    LOG.info(
        "model-canvas: exporting %s CAD layer(s) (full canvas + crop per region each).",
        len(names),
    )
    per_plan_parts: list[list[tuple[str, Path]]] = [[] for _ in clips]
    ok = 1
    for idx, lyr in enumerate(names):
        lyr_safe = sanitize_label(lyr)
        pat = f"^{re.escape(lyr)}$"
        full_path = out_dir / f"{stem}_Layer{idx:03d}_{lyr_safe}_full.pdf"
        LOG.info(
            "model-canvas phase 2: layer %s (%s) full canvas → %s",
            idx,
            lyr,
            full_path.name,
        )
        if not run_dwg2pdf(dwg2pdf, path, full_path, "Model", extra_args, layer_regex=pat):
            continue
        if full_path.stat().st_size < 120 or pdf_page_looks_blank(full_path):
            LOG.info(
                "model-canvas: skip %r — QCAD produced no usable linework on this layer filter.",
                lyr,
            )
            full_path.unlink(missing_ok=True)
            continue
        ok += 1
        for pi, (reg, clip) in enumerate(clips):
            label_part = sanitize_label(reg.label)
            crop_path = (
                out_dir / f"{stem}_Layer{idx:03d}_{lyr_safe}_Plan{pi + 1:02d}_{label_part}.pdf"
            )
            if crop_pdf_page_to_rect(full_path, clip, crop_path):
                LOG.info("model-canvas phase 3: crop → %s", crop_path.name)
                ok += 1
                per_plan_parts[pi].append((lyr, crop_path))
            else:
                LOG.warning("model-canvas: crop failed for layer %r region %s", lyr, reg.label)

    ocg_made = 0
    for pi, (reg, _) in enumerate(clips):
        label_part = sanitize_label(reg.label)
        parts = per_plan_parts[pi]
        if len(parts) < 1:
            continue
        ocg_out = out_dir / f"{stem}_Plan{pi + 1:02d}_{label_part}_ocg.pdf"
        if merge_pdfs_as_ocg(parts, ocg_out):
            _log_ocg_count(ocg_out)
            ocg_made += 1
            ok += 1

    if ocg_made:
        LOG.info("model-canvas: assembled %s per-plan OCG PDF(s) from per-layer crops.", ocg_made)
    return ok


def process_file(
    path: Path,
    out_dir: Path,
    dwg2pdf: Path,
    regions: list[Region],
    mode: str,
    extra_args: list[str],
    source_doc: ezdxf.Drawing | None,
    max_bbox_area: float | None,
    pdf_layers: bool,
    max_pdf_layers: int,
    viewport_margin: float,
    pdf_layers_paperspace: bool,
    model_canvas_all_table_layers: bool = False,
) -> int:
    stem = path.stem
    if mode == "model_canvas":
        return _process_model_canvas(
            path,
            out_dir,
            dwg2pdf,
            regions,
            extra_args,
            source_doc,
            max_pdf_layers,
            stem,
            model_canvas_all_table_layers,
        )
    ok = 0
    for i, reg in enumerate(regions, start=1):
        label_part = sanitize_label(reg.label)
        out_pdf = out_dir / f"{stem}_Plan{i:02d}_{label_part}.pdf"
        tmp: Path | None = None
        tmp_vp_slice: Path | None = None
        try:
            if mode == "layout":
                if pdf_layers:
                    if source_doc is None:
                        LOG.error("--pdf-layers requires a loaded drawing (ODA/read failed?).")
                        continue
                    names = layer_names_capped(cad_layer_names(source_doc), max_pdf_layers)
                    if not names:
                        LOG.error("No layers in drawing for OCG export.")
                        continue
                    vp_raw = None if pdf_layers_paperspace else primary_viewport_model_region(
                        source_doc, reg.label, margin=viewport_margin
                    )
                    vp_reg = vp_raw
                    if vp_reg is not None:
                        msp_ext = bbox.extents(source_doc.modelspace())
                        if msp_ext.has_data and not _region_overlaps_bbox2d(vp_reg, msp_ext):
                            LOG.info(
                                "Layout %s viewport WCS box does not intersect model extents "
                                "(common with some exports); using paper-space OCG plot instead.",
                                reg.label,
                            )
                            vp_reg = None
                    qcad_input = path
                    ocg_block: str | None = reg.label
                    sliced_for_names: ezdxf.Drawing | None = None
                    if pdf_layers_paperspace:
                        LOG.info("OCG mode: paper-space plot per layer (--pdf-layers-paperspace).")
                    elif vp_reg is not None:
                        sliced_for_names = copy_intersecting_to_new_doc(
                            source_doc, vp_reg, max_bbox_area=None
                        )
                        if sliced_for_names is not None:
                            with tempfile.NamedTemporaryFile(
                                suffix=".dxf", delete=False
                            ) as tfv:
                                tmp_vp_slice = Path(tfv.name)
                            sliced_for_names.saveas(tmp_vp_slice)
                            qcad_input = tmp_vp_slice
                            ocg_block = "Model"
                            names = layer_names_capped(
                                cad_layer_names(sliced_for_names), max_pdf_layers
                            )
                            LOG.info(
                                "OCG mode: using Model slice from layout %s viewport "
                                "(%s layers, %s×%s drawing units).",
                                reg.label,
                                len(names),
                                round(vp_reg.max_x - vp_reg.min_x, 3),
                                round(vp_reg.max_y - vp_reg.min_y, 3),
                            )
                        else:
                            LOG.warning(
                                "Viewport slice for layout %s was empty; using paper-space plot for OCG.",
                                reg.label,
                            )
                    elif vp_raw is None:
                        LOG.warning(
                            "No usable VIEWPORT on layout %s; paper-space OCG may miss layers if viewports "
                            "hide them.",
                            reg.label,
                        )
                    if not names:
                        LOG.error("No layers left for OCG export after viewport slice.")
                        continue
                    LOG.info(
                        "OCG mode: exporting %s layer(s) via QCAD then merging (may take a while).",
                        len(names),
                    )
                    with tempfile.TemporaryDirectory(prefix="ocg_layers_") as wd:
                        if not export_plan_pdf_with_ocg_layers(
                            dwg2pdf,
                            qcad_input,
                            ocg_block,
                            names,
                            out_pdf,
                            extra_args,
                            Path(wd),
                        ):
                            continue
                elif not run_dwg2pdf(dwg2pdf, path, out_pdf, reg.label, extra_args):
                    continue
            else:
                doc = source_doc
                if doc is None:
                    LOG.error("Internal error: slice mode without loaded document")
                    continue
                sliced = copy_intersecting_to_new_doc(doc, reg, max_bbox_area)
                if sliced is None:
                    continue
                with tempfile.NamedTemporaryFile(
                    suffix=".dxf", delete=False
                ) as tf:
                    tmp = Path(tf.name)
                sliced.saveas(tmp)
                if pdf_layers:
                    names = layer_names_capped(cad_layer_names(sliced), max_pdf_layers)
                    if not names:
                        LOG.error("Sliced drawing has no layers for OCG export.")
                        continue
                    LOG.info(
                        "OCG mode (slice): %s layer(s) for region %s.",
                        len(names),
                        reg.label,
                    )
                    with tempfile.TemporaryDirectory(prefix="ocg_layers_") as wd:
                        if not export_plan_pdf_with_ocg_layers(
                            dwg2pdf,
                            tmp,
                            "Model",
                            names,
                            out_pdf,
                            extra_args,
                            Path(wd),
                        ):
                            continue
                elif not run_dwg2pdf(dwg2pdf, tmp, out_pdf, "Model", extra_args):
                    continue
            ok += 1
            LOG.info("Wrote %s", out_pdf)
        finally:
            if tmp and tmp.is_file():
                tmp.unlink(missing_ok=True)
            if tmp_vp_slice and tmp_vp_slice.is_file():
                tmp_vp_slice.unlink(missing_ok=True)
    return ok


def modelspace_nonempty(doc: ezdxf.Drawing) -> bool:
    try:
        return len(doc.modelspace()) > 0
    except Exception:
        return True


def collect_regions(
    path: Path,
    strategy: str,
    block_regexes: list[str],
    cluster_eps: float,
    cluster_min: int,
    cluster_margin: float,
    cluster_max_entities: int,
    oda_converter: Path | None,
    oda_version: str,
) -> tuple[ezdxf.Drawing | None, list[Region], str]:
    if strategy == "model-canvas":
        strategy = "model_canvas"
    doc = load_drawing(path, oda_converter=oda_converter, dxf_out_version=oda_version)
    if doc is None:
        return None, [], "slice"

    msp_nonempty = modelspace_nonempty(doc)

    if strategy == "layouts":
        regs = strategy_layouts(doc)
        if not regs and not msp_nonempty:
            LOG.warning("No paper layouts and model space empty: %s", path)
        return doc, regs, "layout"

    if strategy == "blocks":
        patterns = [re.compile(p, re.I) for p in block_regexes]
        regs = strategy_blocks(doc, patterns)
        return doc, regs, "slice"

    if strategy == "cluster":
        regs = strategy_cluster(
            doc, cluster_eps, cluster_min, cluster_margin, cluster_max_entities
        )
        if not regs:
            layout_regs = strategy_layouts(doc)
            if layout_regs:
                LOG.info("Falling back to layout export for %s", path)
                return doc, layout_regs, "layout"
        return doc, regs, "slice"

    if strategy == "model_canvas":
        regs = strategy_cluster(
            doc, cluster_eps, cluster_min, cluster_margin, cluster_max_entities
        )
        if not regs:
            LOG.error(
                "model-canvas: no clusters; try a smaller --cluster-eps or --cluster-min-samples=2."
            )
        return doc, regs, "model_canvas"

    raise ValueError(strategy)


def resolve_max_pdf_layers(
    strategy: str,
    max_pdf_layers: int | None,
    all_pdf_layers: bool,
) -> int:
    """
    ``<= 0`` means no cap (every layer in the table, except skipped names like Defpoints).

    * model-canvas defaults to all layers (one QCAD run per layer).
    * --pdf-layers defaults to 300 when --max-pdf-layers is omitted (safety).
    """
    if all_pdf_layers:
        return 0
    if max_pdf_layers is not None:
        return max_pdf_layers
    if strategy == "model-canvas":
        return 0
    return 300


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Extract floor plans to PDF via QCAD dwg2pdf.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples — one DWG, many PDFs (one per layout / sheet):

  # DWG is converted to out/drawing.dxf via ODA (layer table kept); QCAD plots that DXF.
  python dwg_floorplan_extract.py --file drawing.dwg -o out --strategy layouts

  # Optional: PDF optional content groups per CAD layer (Adobe Acrobat → Layers).
  python dwg_floorplan_extract.py --file drawing.dwg -o out --strategy layouts --pdf-layers

  # Skip writing a persistent DXF; pass DWG straight to QCAD (needs QCAD DWG/Pro).
  python dwg_floorplan_extract.py --file drawing.dwg -o out --no-dwg-preflight

QCAD never writes PDF OCGs by itself; --pdf-layers runs QCAD once per CAD layer and merges with PyMuPDF.

model-canvas (multi-layer canvas pipeline):
  (1) One merged Model PDF (all layers) — your “original canvas” for alignment and QC.
  (2) Floor-plan regions from full modelspace DBSCAN (one bbox per plan; tune --cluster-*).
  (3) One full Model PDF per *used* CAD layer (*_Layer*_full.pdf), then the same region clips applied
      to each (*_Layer*_Plan*_*.pdf). Unused table rows and blank QCAD layer-filter exports are skipped
      by default; use --model-canvas-all-table-layers to force every layer table entry.
  (4) Per plan: *_Plan*_ocg.pdf merges that plan’s layer crops as Acrobat layers.
  Does not require --pdf-layers.
""",
    )
    p.add_argument(
        "-i",
        "--input",
        type=Path,
        default=Path("input"),
        help="Input directory (scanned for .dxf/.dwg)",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output"),
        help="Output directory for PDFs",
    )
    p.add_argument(
        "--dwg2pdf",
        default=None,
        help=f"Path to dwg2pdf (default tries {DEFAULT_DWG2PDF} then {ALT_DWG2PDF})",
    )
    p.add_argument(
        "--oda-converter",
        type=Path,
        default=None,
        help=f"Path to ODAFileConverter executable (default {DEFAULT_ODA})",
    )
    p.add_argument(
        "--oda-dxf-version",
        default="ACAD2013",
        help='Output DXF version when converting .dwg via ODA (e.g. "ACAD2013")',
    )
    p.add_argument(
        "--dxf-work-dir",
        type=Path,
        default=None,
        help="Directory for DWG→DXF files (default: same as -o / --output).",
    )
    p.add_argument(
        "--no-dwg-preflight",
        action="store_true",
        help="Do not write an intermediate DXF; pass .dwg directly to QCAD (requires DWG-capable QCAD).",
    )
    p.add_argument(
        "--force-dwg-convert",
        action="store_true",
        help="Always re-run ODA DWG→DXF even if an up-to-date .dxf exists.",
    )
    p.add_argument(
        "--strategy",
        choices=("layouts", "cluster", "blocks", "model-canvas"),
        default="layouts",
        help="layouts | cluster | blocks | model-canvas (merged canvas → regions → per-layer full PDFs → crop per region)",
    )
    p.add_argument(
        "--block-regex",
        action="append",
        default=[],
        help="For blocks strategy: regex matching block names (repeatable)",
    )
    p.add_argument("--cluster-eps", type=float, default=5000.0, help="DBSCAN eps (drawing units)")
    p.add_argument("--cluster-min-samples", type=int, default=3)
    p.add_argument("--cluster-margin", type=float, default=0.05, help="Margin fraction around cluster")
    p.add_argument("--cluster-max-entities", type=int, default=50_000)
    p.add_argument(
        "--max-bbox-area",
        type=float,
        default=None,
        help="Skip slice regions larger than this area (drawing units²); anomaly guard",
    )
    p.add_argument(
        "--max-plans",
        type=int,
        default=None,
        metavar="N",
        help="Export at most N regions (after detection); useful for smoke tests on large drawings",
    )
    p.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Process single file instead of scanning input directory",
    )
    p.add_argument(
        "--extra-dwg2pdf",
        nargs="*",
        default=[],
        help="Extra args passed to dwg2pdf after built-ins",
    )
    p.add_argument(
        "--pdf-layers",
        "--layered-pdf",
        dest="pdf_layers",
        action="store_true",
        help="Per output PDF: merge one QCAD plot per CAD layer into PDF optional content groups "
        "(toggle in Adobe Acrobat’s Layers panel; slow). Without this flag, each PDF is one "
        "flat composite (normal QCAD behavior).",
    )
    p.add_argument(
        "--max-pdf-layers",
        type=int,
        default=None,
        metavar="N",
        help="Cap how many CAD layers are processed (table order). "
        "Omit for mode defaults: model-canvas uses ALL layers; --pdf-layers uses 300. "
        "Use 0 or --all-pdf-layers for every layer with --pdf-layers.",
    )
    p.add_argument(
        "--all-pdf-layers",
        action="store_true",
        help="Use every CAD layer (same as --max-pdf-layers 0).",
    )
    p.add_argument(
        "--viewport-margin",
        type=float,
        default=0.02,
        help="Fractional padding around layout viewport WCS box when slicing for --pdf-layers",
    )
    p.add_argument(
        "--pdf-layers-paperspace",
        action="store_true",
        help="With --pdf-layers on layouts: use -block=LAYOUT (old behavior). Default: slice viewport to Model.",
    )
    p.add_argument(
        "--model-canvas-all-table-layers",
        action="store_true",
        help="model-canvas: dwg2pdf every layer table row. Default: only layers that have Model geometry "
        "(direct or inside blocks), and skip exports PyMuPDF sees as blank — avoids hundreds of empty canvases.",
    )
    p.add_argument(
        "--pdf-black-lines",
        action="store_true",
        help="After DWG→DXF (ODA has no color options), write *_black.dxf: layer defaults + entities → "
        "ACI 7, true-color stripped; QCAD plots that. Needs DXF (default DWG preflight).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if platform.system() == "Darwin":
        LOG.debug("macOS: ODA File Converter often prints harmless stderr; we rely on output DXF, not ezdxf odafc.readfile().")

    if not args.pdf_layers and args.strategy != "model-canvas":
        LOG.info(
            "Flat PDF mode: each sheet is a single composite vector plot (all visible layers together). "
            "For separate Acrobat/PDF layers per CAD layer, re-run with --pdf-layers (or --layered-pdf)."
        )

    exe = resolve_dwg2pdf(args.dwg2pdf)
    if not exe:
        LOG.error(
            "dwg2pdf not found. Install QCAD and pass --dwg2pdf or place app at standard paths."
        )
        return 1

    oda_path: Path | None = args.oda_converter
    if oda_path is None and Path(DEFAULT_ODA).is_file():
        oda_path = Path(DEFAULT_ODA)

    out_dir = args.output
    out_dir.mkdir(parents=True, exist_ok=True)
    dxf_work_dir = args.dxf_work_dir or out_dir

    if args.file:
        files = [args.file]
    else:
        ind = args.input
        if not ind.is_dir():
            LOG.error("Input directory does not exist: %s", ind)
            return 1
        files = sorted(
            f
            for f in ind.iterdir()
            if f.suffix.lower() in DEFAULT_EXTS and f.is_file()
        )

    if not files:
        LOG.error("No drawing files to process.")
        return 1

    block_regexes = args.block_regex or [r"title|frame|border|plan"]
    max_pdf_layers = resolve_max_pdf_layers(
        args.strategy,
        args.max_pdf_layers,
        args.all_pdf_layers,
    )
    if max_pdf_layers <= 0:
        LOG.info("CAD layer cap: none (all layers in table order, except Defpoints).")
    total = 0
    for f in files:
        LOG.info("Processing %s", f)
        plot_path = resolve_plot_dxf(
            f,
            dxf_work_dir,
            oda_path,
            args.oda_dxf_version,
            preflight=not args.no_dwg_preflight,
            force_convert=args.force_dwg_convert,
        )
        if plot_path is None:
            continue
        if args.pdf_black_lines:
            bl = black_lines_plot_path(plot_path, dxf_work_dir)
            if bl is None:
                continue
            plot_path = bl
        doc, regions, mode = collect_regions(
            plot_path,
            args.strategy,
            block_regexes,
            args.cluster_eps,
            args.cluster_min_samples,
            args.cluster_margin,
            args.cluster_max_entities,
            oda_path,
            args.oda_dxf_version,
        )
        if not regions:
            LOG.error("No regions to export for %s", f)
            continue
        if args.max_plans is not None:
            regions = regions[: args.max_plans]
            LOG.info("Limited to %s region(s) (--max-plans)", len(regions))
        n = process_file(
            plot_path,
            out_dir,
            exe,
            regions,
            mode,
            list(args.extra_dwg2pdf),
            doc,
            args.max_bbox_area,
            pdf_layers=args.pdf_layers,
            max_pdf_layers=max_pdf_layers,
            viewport_margin=args.viewport_margin,
            pdf_layers_paperspace=args.pdf_layers_paperspace,
            model_canvas_all_table_layers=args.model_canvas_all_table_layers,
        )
        total += n
    LOG.info("Done. %s PDF(s) written.", total)
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
