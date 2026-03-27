#!/usr/bin/env python3
"""
Convert DWG to PDF via Autodesk Platform Services (Model Derivative API).

Uses OAuth2 client_credentials, OSS (direct-to-S3 signed upload), SVF2 translation
with ``advanced.2dviews: pdf`` (DWG does not accept a plain ``{"type":"pdf"}`` job),
manifest polling, download of each ``pdf-page`` resource, and PyMuPDF merge.

**Stage 1** (``--stage1``): after translation, downloads **properties.db** from the
manifest and writes distinct **Layer** property values seen in the translated model
(APS-only; not the same as the full CAD LAYER table—layers with no extracted geometry
may be missing).

**PDF layers:** Model Derivative sheet PDFs are **flat vector/raster output**—they do
not preserve CAD layers as Acrobat optional content (OCG). Layered PDFs require
Design Automation (cloud AutoCAD) or another plot path.

DXF output is not supported on this Model Derivative job path. Credentials: ``.aps``.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import fitz
import requests

LOG = logging.getLogger("aps_dwg_convert")

AUTH_URL = "https://developer.api.autodesk.com/authentication/v2/token"
BUCKETS_URL = "https://developer.api.autodesk.com/oss/v2/buckets"
OSS_SIGNED_UPLOAD_TMPL = (
    "https://developer.api.autodesk.com/oss/v2/buckets/{bucket}/objects/{obj}/signeds3upload"
)
JOB_URL = "https://developer.api.autodesk.com/modelderivative/v2/designdata/job"
MANIFEST_TMPL = "https://developer.api.autodesk.com/modelderivative/v2/designdata/{urn}/manifest"
DERIVATIVE_TMPL = (
    "https://developer.api.autodesk.com/modelderivative/v2/designdata/{urn}/manifest/{derivative}"
)

SCOPES = "data:read data:write data:create bucket:create bucket:read"

STAGE1_SCHEMA = "aps_stage1_layers/md_property_db/v1"


def collect_property_database_urn(manifest: dict[str, Any]) -> str | None:
    """URN of ``application/autodesk-db`` (PropertyDatabase) in a successful manifest."""

    def walk(obj: Any) -> str | None:
        if isinstance(obj, dict):
            if obj.get("mime") == "application/autodesk-db" and isinstance(obj.get("urn"), str):
                if obj.get("role") == "Autodesk.CloudPlatform.PropertyDatabase":
                    return obj["urn"]
            for v in obj.values():
                found = walk(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = walk(item)
                if found:
                    return found
        return None

    u = walk(manifest)
    if u:
        return u

    def walk_any_db(obj: Any) -> str | None:
        if isinstance(obj, dict):
            if obj.get("mime") == "application/autodesk-db" and isinstance(obj.get("urn"), str):
                return obj["urn"]
            for v in obj.values():
                found = walk_any_db(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = walk_any_db(item)
                if found:
                    return found
        return None

    return walk_any_db(manifest)


def layer_names_from_properties_db(data: bytes) -> list[str]:
    """
    Distinct Layer property string values from Model Derivative ``properties.db`` (SQLite).

    Schema is internal to Autodesk; we match attribute rows whose name/display_name
    identify the CAD **Layer** field on entities.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tf.write(data)
        path = tf.name
    try:
        conn = sqlite3.connect(path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in cur.fetchall()}
            needed = ("_objects_attr", "_objects_eav", "_objects_val")
            if not all(t in tables for t in needed):
                raise RuntimeError(
                    f"properties.db missing expected tables {needed}; got {sorted(tables)[:20]}…"
                )

            cur.execute("PRAGMA table_info(_objects_val)")
            val_cols = [r[1] for r in cur.fetchall()]
            value_col = next(
                (c for c in ("value", "val", "s", "text") if c in val_cols),
                val_cols[-1] if val_cols else "value",
            )

            cur.execute(
                "SELECT id, name, category, display_name FROM _objects_attr",
            )
            attr_ids: set[int] = set()
            for row in cur.fetchall():
                aid, name, _, disp = row[0], row[1] or "", row[2] or "", row[3] or ""
                n = name.strip().lower()
                d = (disp or "").strip().lower()
                if n == "layer" or d == "layer":
                    attr_ids.add(int(aid))

            if not attr_ids:
                cur.execute(
                    "SELECT id FROM _objects_attr WHERE "
                    "lower(name) = 'layer' OR lower(ifnull(display_name,'')) = 'layer'"
                )
                attr_ids = {int(r[0]) for r in cur.fetchall()}

            if not attr_ids:
                return []

            qmarks = ",".join("?" * len(attr_ids))
            cur.execute(
                f"SELECT DISTINCT v.[{value_col}] FROM _objects_eav e "
                f"JOIN _objects_val v ON v.id = e.value_id "
                f"WHERE e.attribute_id IN ({qmarks})",
                list(attr_ids),
            )
            out: set[str] = set()
            for (val,) in cur.fetchall():
                if val is None:
                    continue
                s = str(val).strip()
                if s:
                    out.add(s)
            return sorted(out, key=str.casefold)
        finally:
            conn.close()
    finally:
        Path(path).unlink(missing_ok=True)


def write_stage1_layers_json(
    source_drawing: Path,
    layer_names: list[str],
    out_json: Path,
) -> None:
    payload = {
        "schema": STAGE1_SCHEMA,
        "source": str(source_drawing.resolve()),
        "layer_table": layer_names,
        "count": len(layer_names),
        "note": (
            "Distinct 'Layer' property values from Model Derivative properties.db after SVF2 "
            "translation—not guaranteed to match the full CAD LAYER table (e.g. unused layers)."
        ),
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    LOG.info("Wrote APS layer list (%d names): %s", len(layer_names), out_json)


def load_credentials(aps_path: Path) -> tuple[str, str]:
    """Parse ``client_credentials = 'CLIENT_ID:CLIENT_SECRET'`` from ``.aps``."""
    if not aps_path.is_file():
        raise FileNotFoundError(aps_path)
    text = aps_path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(
            f"{aps_path} is empty. Save your APS app credentials as "
            "client_credentials = 'CLIENT_ID:CLIENT_SECRET' (single line)."
        )
    m = re.match(
        r'client_credentials\s*=\s*[\'"]([^\'"]+)[\'"]',
        text,
    )
    if not m:
        raise ValueError(f"Could not parse client_credentials from {aps_path}")
    pair = m.group(1)
    if ":" not in pair:
        raise ValueError("Expected CLIENT_ID:CLIENT_SECRET inside quotes")
    client_id, _, secret = pair.partition(":")
    if not client_id or not secret:
        raise ValueError("CLIENT_ID and CLIENT_SECRET must be non-empty")
    return client_id, secret


def get_access_token(client_id: str, client_secret: str) -> str:
    r = requests.post(
        AUTH_URL,
        headers={"Accept": "application/json"},
        data={
            "grant_type": "client_credentials",
            "scope": SCOPES,
        },
        auth=(client_id, client_secret),
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in response: {data}")
    return token


def ensure_bucket(token: str, bucket_key: str) -> None:
    r = requests.post(
        BUCKETS_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"bucketKey": bucket_key, "policyKey": "transient"},
        timeout=120,
    )
    if r.status_code == 200 or r.status_code == 201:
        LOG.info("Created bucket %s", bucket_key)
        return
    if r.status_code == 409:
        LOG.info("Bucket %s already exists", bucket_key)
        return
    r.raise_for_status()


def upload_object(token: str, bucket_key: str, object_name: str, file_path: Path) -> str:
    """
    Upload via direct-to-S3 signed URLs. The legacy ``PUT .../objects/:key`` endpoint
    is deprecated (403 Legacy endpoint is deprecated).
    """
    enc_key = quote(object_name, safe="")
    base = OSS_SIGNED_UPLOAD_TMPL.format(bucket=bucket_key, obj=enc_key)
    # Single-part upload (default parts=1). Longer URL lifetime for large files.
    r0 = requests.get(
        base,
        params={"parts": 1, "minutesExpiration": 30},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=120,
    )
    r0.raise_for_status()
    up = r0.json()
    upload_key = up.get("uploadKey")
    urls = up.get("urls") or []
    if not upload_key or not urls:
        raise RuntimeError(f"Unexpected signeds3upload response: {up}")

    with file_path.open("rb") as f:
        body = f.read()

    put_url = urls[0]
    r1 = requests.put(
        put_url,
        data=body,
        headers={"Content-Type": "application/octet-stream"},
        timeout=600,
    )
    r1.raise_for_status()

    r2 = requests.post(
        base,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-ads-meta-Content-Type": "application/octet-stream",
        },
        json={"uploadKey": upload_key},
        timeout=120,
    )
    r2.raise_for_status()
    data = r2.json()
    oid = data.get("objectId")
    if not oid:
        raise RuntimeError(f"No objectId in complete upload response: {data}")
    LOG.info("Uploaded %s → %s", file_path, oid)
    return oid


def object_id_to_design_urn(object_id: str) -> str:
    """URL-safe Base64 URN required by Model Derivative (no padding)."""
    b = base64.b64encode(object_id.encode("utf-8")).decode("ascii")
    return b.replace("+", "-").replace("/", "_").rstrip("=")


def submit_translation_job(token: str, design_urn_b64: str, out_format: str) -> None:
    fmt = out_format.lower()
    if fmt != "pdf":
        raise ValueError("Internal error: only pdf jobs are supported.")
    # Direct {"type":"pdf"} jobs fail for DWG with "Failed to trigger translation". DWG 2D PDFs are
    # produced as part of SVF2 translation with advanced 2dviews (see APS blog: RVT/DWG 2D views).
    payload = {
        "input": {"urn": design_urn_b64},
        "output": {
            "destination": {"region": "us"},
            "formats": [
                {
                    "type": "svf2",
                    "views": ["2d", "3d"],
                    "advanced": {"2dviews": "pdf"},
                }
            ],
        },
    }
    r = requests.post(
        JOB_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    LOG.info("Translation job submitted (svf2 + 2d PDF pages)")


def poll_manifest(
    token: str,
    design_urn_b64: str,
    interval_sec: float = 3.0,
    max_wait_sec: float = 600.0,
) -> dict[str, Any]:
    url = MANIFEST_TMPL.format(urn=design_urn_b64)
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + max_wait_sec
    while time.monotonic() < deadline:
        r = requests.get(url, headers=headers, timeout=120)
        if r.status_code == 202:
            LOG.info("Manifest not ready yet (202), waiting…")
            time.sleep(interval_sec)
            continue
        if r.status_code == 404:
            LOG.info("Manifest not found yet (404), waiting…")
            time.sleep(interval_sec)
            continue
        r.raise_for_status()
        manifest = r.json()
        status = manifest.get("status", "").lower()
        progress = manifest.get("progress", "")
        if status in ("failed", "timeout"):
            raise RuntimeError(f"Translation failed: status={status!r} progress={progress!r}")
        if status == "success":
            return manifest
        LOG.info("Manifest status=%s progress=%s, waiting…", status, progress)
        time.sleep(interval_sec)
    raise TimeoutError(f"Manifest did not reach success within {max_wait_sec}s")


def collect_pdf_page_urns(manifest: dict[str, Any]) -> list[str]:
    """Collect URNs for DWG 2D PDF resources (role *pdf-page*, mime *application/pdf*)."""
    out: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if (
                obj.get("mime") == "application/pdf"
                and obj.get("role") == "pdf-page"
                and isinstance(obj.get("urn"), str)
            ):
                out.append(obj["urn"])
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(manifest)
    if not out:
        raise RuntimeError(
            "No pdf-page resources in manifest (expected SVF2 job with advanced 2dviews=pdf)."
        )
    return out


def download_derivative_bytes(token: str, design_urn_b64: str, derivative_urn: str) -> bytes:
    enc = quote(derivative_urn, safe="")
    url = DERIVATIVE_TMPL.format(urn=design_urn_b64, derivative=enc)
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=600,
    )
    r.raise_for_status()
    return r.content


def merge_pdf_bytes(parts: list[bytes], out_path: Path) -> None:
    merged = fitz.open()
    try:
        for chunk in parts:
            with fitz.open(stream=chunk, filetype="pdf") as src:
                merged.insert_pdf(src)
        page_count = merged.page_count
        merged.save(out_path.as_posix())
    finally:
        merged.close()
    LOG.info(
        "Wrote merged PDF %s (%d pages, %d sheet PDF(s))",
        out_path,
        page_count,
        len(parts),
    )
    LOG.info(
        "Model Derivative PDFs have no CAD/PDF optional layers (flat output). "
        "Layered PDFs need Design Automation (AutoCAD) or another plot pipeline."
    )


def convert_dwg(
    dwg_path: Path,
    out_path: Path,
    out_format: str,
    aps_path: Path,
    bucket_key: str | None = None,
    stage1_layers_json: Path | None = None,
) -> None:
    if out_format.lower() == "dxf":
        raise ValueError(
            "Model Derivative API v2 does not support output type 'dxf'. "
            "Use Design Automation for AutoCAD or a desktop converter for DWG→DXF."
        )
    client_id, secret = load_credentials(aps_path)
    token = get_access_token(client_id, secret)
    bkey = bucket_key or f"dwg-{uuid.uuid4().hex[:24]}"
    ensure_bucket(token, bkey)
    object_name = dwg_path.name
    object_id = upload_object(token, bkey, object_name, dwg_path)
    design_urn = object_id_to_design_urn(object_id)
    submit_translation_job(token, design_urn, out_format)
    manifest = poll_manifest(token, design_urn)
    if stage1_layers_json is not None:
        pdb_urn = collect_property_database_urn(manifest)
        if not pdb_urn:
            raise RuntimeError(
                "No properties.db (PropertyDatabase) in manifest; cannot list layers from APS."
            )
        LOG.info("Downloading properties.db for layer list…")
        prop_bytes = download_derivative_bytes(token, design_urn, pdb_urn)
        layer_names = layer_names_from_properties_db(prop_bytes)
        if not layer_names:
            LOG.warning(
                "No Layer values found in properties.db (unexpected for DWG); JSON will be empty."
            )
        write_stage1_layers_json(dwg_path, layer_names, stage1_layers_json)
    if out_format.lower() == "pdf":
        pdf_urns = collect_pdf_page_urns(manifest)
        LOG.info("Downloading %d PDF sheet(s)…", len(pdf_urns))
        blobs = [download_derivative_bytes(token, design_urn, u) for u in pdf_urns]
        merge_pdf_bytes(blobs, out_path)


def main() -> int:
    p = argparse.ArgumentParser(description="Convert DWG to PDF or DXF via APS Model Derivative.")
    p.add_argument(
        "--input",
        type=Path,
        default=Path("example.dwg"),
        help="Path to input .dwg",
    )
    p.add_argument(
        "--output",
        type=Path,
        help="Output file path (default: <input_stem>.pdf or .dxf)",
    )
    p.add_argument(
        "--format",
        choices=("pdf", "dxf"),
        default="pdf",
        help="Output format: pdf (supported). dxf is not supported by Model Derivative (exits with error).",
    )
    p.add_argument(
        "--aps",
        type=Path,
        default=Path(".aps"),
        help="Path to .aps credentials file",
    )
    p.add_argument(
        "--bucket-key",
        help="Optional fixed OSS bucket key (must be globally unique if new)",
    )
    p.add_argument(
        "--stage1",
        action="store_true",
        help="Stage 1: after translation, write JSON of Layer values from APS properties.db "
        "(not the full CAD layer table). Same APS run as the PDF.",
    )
    p.add_argument(
        "--layers-out",
        type=Path,
        help="Path for stage-1 layer JSON (default: <output_stem>_layers.json next to --output)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    inp = args.input.expanduser().resolve()
    if not inp.is_file():
        LOG.error("Input not found: %s", inp)
        return 1
    out = args.output
    if out is None:
        out = inp.with_suffix(f".{args.format}")
    else:
        out = out.expanduser().resolve()
    layers_json: Path | None = None
    if args.stage1:
        layers_json = args.layers_out
        if layers_json is None:
            layers_json = out.parent / f"{out.stem}_layers.json"
        else:
            layers_json = layers_json.expanduser().resolve()
    try:
        convert_dwg(
            inp,
            out,
            args.format,
            args.aps.expanduser().resolve(),
            bucket_key=args.bucket_key,
            stage1_layers_json=layers_json,
        )
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        LOG.error("HTTP %s: %s", e.response.status_code if e.response else "?", body[:2000])
        return 1
    except Exception as e:
        LOG.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
