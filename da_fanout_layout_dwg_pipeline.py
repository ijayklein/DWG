#!/usr/bin/env python3
"""
Fan-out Design Automation pipeline — one **full-geometry DWG per layout** via ``-EXPORTLAYOUT``.

Two-phase approach:
  1. **ListLayoutNames** WorkItem → ``layout_names.json`` (layout tab names from the DWG).
  2. **ExportSingleLayoutDwg** × N WorkItems (one per layout, parallel) → each produces a
     single-DWG zip. Collected into one ``layout_dwgs.zip``.

This avoids the cumulative viewport-regen crash that occurs when looping ``-EXPORTLAYOUT``
over 27+ layouts in a single AcCoreConsole session.

Usage::

    python da_fanout_layout_dwg_pipeline.py --input example.dwg --output layout_dwgs.zip --aps .aps

Env / defaults:
    DA_LIST_LAYOUTS_ACTIVITY_ID   (or hardcoded DEFAULT below)
    DA_SINGLE_LAYOUT_DWG_ACTIVITY_ID
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import tempfile
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from aps_dwg_convert import ensure_bucket, load_credentials, upload_object

LOG = logging.getLogger("da_fanout_layout_dwg")

NICKNAME = "0PWFCWGmSuGYmAVHOOm1OFAzaZPqHxobLYL7PqEtPtmwE52a"

DEFAULT_LIST_LAYOUTS_ACTIVITY_ID: str = os.environ.get(
    "DA_LIST_LAYOUTS_ACTIVITY_ID",
    f"{NICKNAME}.ListLayoutNamesActivity+prod",
).strip()

DEFAULT_SINGLE_LAYOUT_DWG_ACTIVITY_ID: str = os.environ.get(
    "DA_SINGLE_LAYOUT_DWG_ACTIVITY_ID",
    f"{NICKNAME}.SingleLayoutDwgActivity+prod",
).strip()

DEFAULT_SINGLE_LAYOUT_PDF_ACTIVITY_ID: str = os.environ.get(
    "DA_SINGLE_LAYOUT_PDF_ACTIVITY_ID",
    f"{NICKNAME}.SingleLayoutPdfActivity+prod",
).strip()

AUTH_URL = "https://developer.api.autodesk.com/authentication/v2/token"
OSS_SIGNED_UPLOAD_TMPL = (
    "https://developer.api.autodesk.com/oss/v2/buckets/{bucket}/objects/{obj}/signeds3upload"
)
OSS_SIGNED_DOWNLOAD_TMPL = (
    "https://developer.api.autodesk.com/oss/v2/buckets/{bucket}/objects/{obj}/signeds3download"
)
DA_BASE = "https://developer.api.autodesk.com/da/us-east/v3"
DA_SCOPES = "code:all data:read data:write data:create bucket:create bucket:read bucket:delete"


# ── helpers ────────────────────────────────────────────────────────────────────

def load_aps_credentials(aps_path: Path | None = None) -> tuple[str, str]:
    cid = os.environ.get("APS_CLIENT_ID", "").strip()
    sec = os.environ.get("APS_CLIENT_SECRET", "").strip()
    if cid and sec:
        return cid, sec
    p = (aps_path or Path(os.environ.get("APS_CREDENTIALS_PATH", ".aps"))).expanduser().resolve()
    return load_credentials(p)


def get_da_token(client_id: str, client_secret: str) -> str:
    r = requests.post(
        AUTH_URL,
        data={"grant_type": "client_credentials", "scope": DA_SCOPES},
        auth=(client_id, client_secret),
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def signed_upload_url(token: str, bucket: str, obj: str) -> tuple[str, str, str]:
    enc = quote(obj, safe="")
    base = OSS_SIGNED_UPLOAD_TMPL.format(bucket=bucket, obj=enc)
    r = requests.get(
        base,
        params={"parts": 1, "minutesExpiration": 30},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=120,
    )
    r.raise_for_status()
    d = r.json()
    return d["urls"][0], d["uploadKey"], base


def complete_upload(token: str, post_base: str, upload_key: str) -> None:
    requests.post(
        post_base,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "x-ads-meta-Content-Type": "application/octet-stream"},
        json={"uploadKey": upload_key},
        timeout=120,
    ).raise_for_status()


def signed_download_url(token: str, bucket: str, obj: str) -> str:
    enc = quote(obj, safe="")
    r = requests.get(
        OSS_SIGNED_DOWNLOAD_TMPL.format(bucket=bucket, obj=enc),
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    r.raise_for_status()
    d = r.json()
    return d.get("url") or d["urls"][0]


def download_bytes(token: str, bucket: str, obj: str) -> bytes:
    url = signed_download_url(token, bucket, obj)
    r = requests.get(url, timeout=600)
    r.raise_for_status()
    return r.content


def upload_bytes(token: str, bucket: str, obj: str, data: bytes) -> None:
    put_url, upload_key, post_base = signed_upload_url(token, bucket, obj)
    requests.put(put_url, data=data, headers={"Content-Type": "application/octet-stream"}, timeout=600).raise_for_status()
    complete_upload(token, post_base, upload_key)


def create_workitem(token: str, activity_id: str, arguments: dict[str, Any]) -> str:
    r = requests.post(
        f"{DA_BASE}/workitems",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"activityId": activity_id, "arguments": arguments},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["id"]


def poll_workitem(token: str, wid: str, interval: float = 3.0, max_wait: float = 600.0) -> dict:
    url = f"{DA_BASE}/workitems/{quote(wid, safe='')}"
    deadline = time.monotonic() + max_wait
    in_progress = {"pending", "downloaded", "inprogress", "waitingforupload", "pendinguploads"}
    while time.monotonic() < deadline:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=120)
        r.raise_for_status()
        st = r.json()
        status = (st.get("status") or "").lower()
        LOG.info("  WorkItem %s → %s", wid, status)
        if status == "success":
            return st
        if status not in in_progress:
            return st
        time.sleep(interval)
    raise TimeoutError(wid)


def fetch_report(result: dict) -> str:
    url = result.get("reportUrl", "")
    if not url:
        return ""
    try:
        r = requests.get(url, timeout=120)
        return r.text[:12000] if r.ok else ""
    except OSError:
        return ""


# ── phase 1: list layouts ─────────────────────────────────────────────────────

def phase_list_layouts(
    token: str,
    bucket: str,
    host_urn: str,
    dll_urn: str,
    deps_urn: str,
    activity_id: str,
) -> list[str]:
    LOG.info("Phase 1: ListLayoutNames")
    auth = f"Bearer {token}"
    out_name = "layout_names.json"
    put_url, upload_key, post_base = signed_upload_url(token, bucket, out_name)

    args = {
        "HostDwg": {"url": host_urn, "verb": "get", "headers": {"Authorization": auth}},
        "PluginDll": {"url": dll_urn, "verb": "get", "headers": {"Authorization": auth}},
        "PluginDeps": {"url": deps_urn, "verb": "get", "headers": {"Authorization": auth}},
        "ResultJson": {"url": put_url, "verb": "put"},
    }
    wid = create_workitem(token, activity_id, args)
    result = poll_workitem(token, wid)
    if (result.get("status") or "").lower() != "success":
        report = fetch_report(result)
        raise RuntimeError(f"ListLayoutNames failed: {result.get('status')}\n{report}")

    complete_upload(token, post_base, upload_key)
    data = download_bytes(token, bucket, out_name)
    names: list[str] = json.loads(data)
    LOG.info("  Layouts: %s", names)
    return names


# ── phase 2: fan-out single layout exports ─────────────────────────────────────

def _run_single_layout(
    token: str,
    bucket: str,
    host_urn: str,
    dll_urn: str,
    deps_urn: str,
    activity_id: str,
    layout_name: str,
    idx: int,
) -> tuple[str, bytes | None, str]:
    """Returns (layout_name, zip_bytes_or_None, error_msg)."""
    auth = f"Bearer {token}"
    safe = layout_name.replace("/", "_").replace("\\", "_")
    param_obj = f"params/layout_name_{idx}.txt"
    upload_bytes(token, bucket, param_obj, layout_name.encode("utf-8"))
    param_urn = f"urn:adsk.objects:os.object:{bucket}/{quote(param_obj, safe='')}"

    out_obj = f"results/layout_{idx}.zip"
    put_url, upload_key, post_base = signed_upload_url(token, bucket, out_obj)

    args = {
        "HostDwg": {"url": host_urn, "verb": "get", "headers": {"Authorization": auth}},
        "PluginDll": {"url": dll_urn, "verb": "get", "headers": {"Authorization": auth}},
        "PluginDeps": {"url": deps_urn, "verb": "get", "headers": {"Authorization": auth}},
        "LayoutName": {"url": param_urn, "verb": "get", "headers": {"Authorization": auth}},
        "ResultZip": {"url": put_url, "verb": "put"},
    }
    try:
        wid = create_workitem(token, activity_id, args)
        result = poll_workitem(token, wid)
        if (result.get("status") or "").lower() != "success":
            report = fetch_report(result)
            return layout_name, None, f"WorkItem failed ({result.get('status')}): {report[:2000]}"
        complete_upload(token, post_base, upload_key)
        data = download_bytes(token, bucket, out_obj)
        return layout_name, data, ""
    except Exception as exc:
        return layout_name, None, str(exc)


def phase_export_layouts(
    token: str,
    bucket: str,
    host_urn: str,
    dll_urn: str,
    deps_urn: str,
    activity_id: str,
    layout_names: list[str],
    max_parallel: int = 5,
) -> dict[str, bytes]:
    LOG.info("Phase 2: ExportSingleLayoutDwg × %d layouts (parallel=%d)", len(layout_names), max_parallel)
    results: dict[str, bytes] = {}
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(
                _run_single_layout, token, bucket, host_urn, dll_urn, deps_urn,
                activity_id, name, i,
            ): name
            for i, name in enumerate(layout_names)
        }
        for fut in as_completed(futures):
            name, data, err = fut.result()
            if err:
                LOG.error("  Layout %r FAILED: %s", name, err[:500])
                errors.append(f"{name}: {err[:500]}")
            elif data:
                results[name] = data
                LOG.info("  Layout %r OK (%d bytes zip)", name, len(data))

    if errors:
        LOG.warning("%d layout(s) failed: %s", len(errors), "; ".join(e[:80] for e in errors))
    return results


# ── assemble final zip ─────────────────────────────────────────────────────────

def assemble_zip(layout_zips: dict[str, bytes], file_ext: str = ".dwg") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for layout_name, zip_data in sorted(layout_zips.items()):
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zin:
                for info in zin.infolist():
                    if info.filename.lower().endswith(file_ext.lower()):
                        zout.writestr(info.filename, zin.read(info))
    return buf.getvalue()


# ── main ───────────────────────────────────────────────────────────────────────

def _default_bundle_contents_dir() -> Path:
    return (
        Path(__file__).resolve().parent
        / "design_automation"
        / "LayerPdfExport"
        / "LayerPdfExport.bundle"
        / "Contents"
    )


def run_fanout_pipeline(
    dwg_path: Path,
    out_zip: Path,
    aps_path: Path,
    list_activity: str,
    single_activity: str,
    bucket_key: str | None = None,
    plugin_dll: Path | None = None,
    plugin_deps: Path | None = None,
    max_parallel: int = 5,
    file_ext: str = ".dwg",
) -> None:
    cid, sec = load_aps_credentials(aps_path)
    token = get_da_token(cid, sec)
    bkey = bucket_key or f"da-{uuid.uuid4().hex[:20]}"
    ensure_bucket(token, bkey)

    bundle_dir = _default_bundle_contents_dir()
    dll_path = plugin_dll or (bundle_dir / "LayerPdfExport.dll")
    deps_path = plugin_deps or (bundle_dir / "LayerPdfExport.deps.json")
    if not dll_path.is_file():
        raise FileNotFoundError(f"DLL not found: {dll_path}")
    if not deps_path.is_file():
        raise FileNotFoundError(f"deps.json not found: {deps_path}")

    host_urn = upload_object(token, bkey, dwg_path.name, dwg_path)
    dll_urn = upload_object(token, bkey, "workitem/LayerPdfExport.dll", dll_path)
    deps_urn = upload_object(token, bkey, "workitem/LayerPdfExport.deps.json", deps_path)

    layout_names = phase_list_layouts(token, bkey, host_urn, dll_urn, deps_urn, list_activity)
    if not layout_names:
        raise RuntimeError("No layouts found in DWG")

    layout_zips = phase_export_layouts(
        token, bkey, host_urn, dll_urn, deps_urn,
        single_activity, layout_names, max_parallel,
    )
    if not layout_zips:
        raise RuntimeError("All layout exports failed")

    final = assemble_zip(layout_zips, file_ext=file_ext)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    out_zip.write_bytes(final)
    LOG.info("Saved %s (%d bytes, %d/%d layouts)", out_zip, len(final), len(layout_zips), len(layout_names))


def main() -> int:
    p = argparse.ArgumentParser(
        description="Fan-out DA pipeline: full-geometry DWG per layout via -EXPORTLAYOUT."
    )
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--aps", type=Path, default=Path(".aps"))
    p.add_argument("--list-activity", default=None)
    p.add_argument("--single-activity", default=None)
    p.add_argument("--bucket-key", default=None)
    p.add_argument("--plugin-dll", type=Path, default=None)
    p.add_argument("--plugin-deps", type=Path, default=None)
    p.add_argument("--max-parallel", type=int, default=5)
    p.add_argument(
        "--file-ext",
        default=".dwg",
        help="File extension to extract from each WorkItem ZIP (.dwg or .pdf, default: .dwg)",
    )
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    list_act = (args.list_activity or DEFAULT_LIST_LAYOUTS_ACTIVITY_ID).strip()
    single_act = (args.single_activity or DEFAULT_SINGLE_LAYOUT_DWG_ACTIVITY_ID).strip()
    inp = args.input.expanduser().resolve()
    if not inp.is_file():
        LOG.error("Input not found: %s", inp)
        return 1

    try:
        run_fanout_pipeline(
            inp,
            args.output.expanduser().resolve(),
            args.aps.expanduser().resolve(),
            list_act,
            single_act,
            args.bucket_key,
            plugin_dll=args.plugin_dll.expanduser().resolve() if args.plugin_dll else None,
            plugin_deps=args.plugin_deps.expanduser().resolve() if args.plugin_deps else None,
            max_parallel=args.max_parallel,
            file_ext=args.file_ext,
        )
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        LOG.error("HTTP %s: %s", e.response.status_code if e.response else "?", body[:4000])
        return 1
    except Exception as e:
        LOG.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
