#!/usr/bin/env python3
"""
Design Automation — run your registered **LayerPdfExport** Activity (PDF(s) → ``layer_pdfs.zip``).

Prerequisites (once):
  1. Build ``design_automation/LayerPdfExport`` (GitHub Actions artifact or ``dotnet build`` on Windows).
  2. Register the AppBundle + Activity with APS Design Automation (nickname, engine).
  3. Pass ``--activity-id YourNick.LayerPdfExportActivity+prod``, env ``DA_ACTIVITY_ID``, or set
     ``DEFAULT_DA_ACTIVITY_ID`` in this file for your registered Activity.

Runtime (every run):
  Uploads the DWG to OSS, starts a WorkItem, waits, completes the S3 upload handshake,
  downloads ``layer_pdfs.zip``.

Reuses OAuth + OSS helpers from ``aps_dwg_convert`` (same ``.aps`` file).
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

import os

from aps_dwg_convert import (
    ensure_bucket,
    load_credentials,
    upload_object,
)

LOG = logging.getLogger("da_layer_pdf_pipeline")

# Default qualified activity id when ``--activity-id`` and ``DA_ACTIVITY_ID`` are unset.
# Override per machine or project: ``export DA_ACTIVITY_ID=...`` or pass ``--activity-id``.
DEFAULT_DA_ACTIVITY_ID: str = (
    "0PWFCWGmSuGYmAVHOOm1OFAzaZPqHxobLYL7PqEtPtmwE52a.LayerPdfExportActivity+prod"
)


def load_aps_credentials(
    aps_path: Path | None = None,
    *,
    fallback_client_id: str | None = None,
    fallback_client_secret: str | None = None,
) -> tuple[str, str]:
    """
    Resolve credentials in order:

    1. ``APS_CLIENT_ID`` + ``APS_CLIENT_SECRET`` (e.g. Railway).
    2. Optional ``fallback_client_id`` + ``fallback_client_secret`` (e.g. webapp preconfig).
    3. ``APS_CREDENTIALS_PATH`` or ``.aps`` via ``aps_dwg_convert.load_credentials``.
    """
    cid = os.environ.get("APS_CLIENT_ID", "").strip()
    sec = os.environ.get("APS_CLIENT_SECRET", "").strip()
    if cid and sec:
        return cid, sec
    fc = (fallback_client_id or "").strip()
    fs = (fallback_client_secret or "").strip()
    if fc and fs:
        return fc, fs
    p = (aps_path or Path(os.environ.get("APS_CREDENTIALS_PATH", ".aps"))).expanduser().resolve()
    return load_credentials(p)


AUTH_URL = "https://developer.api.autodesk.com/authentication/v2/token"
OSS_SIGNED_UPLOAD_TMPL = (
    "https://developer.api.autodesk.com/oss/v2/buckets/{bucket}/objects/{obj}/signeds3upload"
)
OSS_SIGNED_DOWNLOAD_TMPL = (
    "https://developer.api.autodesk.com/oss/v2/buckets/{bucket}/objects/{obj}/signeds3download"
)
DA_BASE = "https://developer.api.autodesk.com/da/us-east/v3"

DA_SCOPES = (
    "code:all data:read data:write data:create bucket:create bucket:read bucket:delete"
)


def get_da_token(client_id: str, client_secret: str) -> str:
    r = requests.post(
        AUTH_URL,
        headers={"Accept": "application/json"},
        data={"grant_type": "client_credentials", "scope": DA_SCOPES},
        auth=(client_id, client_secret),
        timeout=120,
    )
    r.raise_for_status()
    t = r.json().get("access_token")
    if not t:
        raise RuntimeError("No access_token")
    return t


def prepare_put_url_for_new_object(
    token: str, bucket_key: str, object_name: str
) -> tuple[str, str, str]:
    """
    Returns (s3_put_url, upload_key, signeds3upload_post_url_base) for OSS direct upload.
    After an external client (Design Automation) PUTs the object bytes, call
    ``complete_signed_upload`` with the same *upload_key*.
    """
    enc = quote(object_name, safe="")
    base = OSS_SIGNED_UPLOAD_TMPL.format(bucket=bucket_key, obj=enc)
    r = requests.get(
        base,
        params={"parts": 1, "minutesExpiration": 30},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=120,
    )
    r.raise_for_status()
    up = r.json()
    upload_key = up["uploadKey"]
    urls = up.get("urls") or []
    if not upload_key or not urls:
        raise RuntimeError(f"signeds3upload: {up}")
    return urls[0], upload_key, base


def complete_signed_upload(token: str, post_base: str, upload_key: str) -> None:
    r = requests.post(
        post_base,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-ads-meta-Content-Type": "application/octet-stream",
        },
        json={"uploadKey": upload_key},
        timeout=120,
    )
    r.raise_for_status()


def get_signed_s3_get_url(token: str, bucket_key: str, object_name: str) -> str:
    """
    Presigned S3 URL for an OSS object. Design Automation **HostDwg** must use this (or a direct
    OSS HTTPS URL), not the ``urn:adsk.objects:…`` objectId — otherwise the worker hits
    **failedDownload**.

    Note: ``signeds3download`` does not accept the same ``minutesExpiration`` query params as
    ``signeds3upload``; use API defaults unless documented otherwise.
    """
    enc = quote(object_name, safe="")
    url = OSS_SIGNED_DOWNLOAD_TMPL.format(bucket=bucket_key, obj=enc)
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    r.raise_for_status()
    du = r.json()
    get_url = du.get("url") or (du.get("urls") or [None])[0]
    if not get_url:
        raise RuntimeError(f"signeds3download: {du}")
    return get_url


def download_object_bytes(token: str, bucket_key: str, object_name: str) -> bytes:
    get_url = get_signed_s3_get_url(token, bucket_key, object_name)
    r2 = requests.get(get_url, timeout=600)
    r2.raise_for_status()
    return r2.content


def create_workitem(
    token: str,
    activity_id: str,
    host_dwg_url: str,
    plugin_dll_urn: str,
    plugin_deps_urn: str,
    output_put_url: str,
    *,
    host_is_oss_urn: bool = True,
    output_is_presigned_s3_put: bool = True,
) -> str:
    """
    Argument names must match the registered Activity (HostDwg, PluginDll, PluginDeps, ResultZip).

    **HostDwg / PluginDll / PluginDeps:** OSS ``objectId`` URNs + ``Authorization: Bearer``.

    **ResultZip:** S3 **presigned PUT** URL with **no** Forge headers.
    """
    auth = f"Bearer {token}"
    host_headers: dict[str, str] = (
        {"Authorization": auth} if host_is_oss_urn else {}
    )
    out_headers: dict[str, str] = {} if output_is_presigned_s3_put else {"Authorization": auth}
    body = {
        "activityId": activity_id,
        "arguments": {
            "HostDwg": {
                "url": host_dwg_url,
                "verb": "get",
                **({"headers": host_headers} if host_headers else {}),
            },
            "PluginDll": {
                "url": plugin_dll_urn,
                "verb": "get",
                "headers": host_headers,
            },
            "PluginDeps": {
                "url": plugin_deps_urn,
                "verb": "get",
                "headers": host_headers,
            },
            "ResultZip": {
                "url": output_put_url,
                "verb": "put",
                **({"headers": out_headers} if out_headers else {}),
            },
        },
    }
    r = requests.post(
        f"{DA_BASE}/workitems",
        headers={"Authorization": auth, "Content-Type": "application/json"},
        json=body,
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    wid = data.get("id")
    if not wid:
        raise RuntimeError(f"workitem: {data}")
    return wid


def poll_workitem(token: str, workitem_id: str, interval: float = 3.0, max_wait: float = 1800.0) -> dict[str, Any]:
    url = f"{DA_BASE}/workitems/{quote(workitem_id, safe='')}"
    deadline = time.monotonic() + max_wait
    auth = f"Bearer {token}"
    # Statuses still running (not exhaustive; anything else is treated as terminal).
    in_progress = {"pending", "downloaded", "inprogress", "waitingforupload", "pendinguploads"}
    while time.monotonic() < deadline:
        r = requests.get(url, headers={"Authorization": auth}, timeout=120)
        r.raise_for_status()
        st = r.json()
        status = (st.get("status") or "").lower()
        LOG.info("WorkItem %s → %s", workitem_id, status)
        if status == "success":
            return st
        if status in in_progress:
            time.sleep(interval)
            continue
        # failedDownload, failedUpload, failedInstructions, failed, cancelled, unknown terminal
        return st
    raise TimeoutError(workitem_id)


def _default_bundle_contents_dir() -> Path:
    return (
        Path(__file__).resolve().parent
        / "design_automation"
        / "LayerPdfExport"
        / "LayerPdfExport.bundle"
        / "Contents"
    )


def run_pipeline(
    dwg_path: Path,
    out_zip: Path,
    aps_path: Path,
    activity_id: str,
    bucket_key: str | None = None,
    *,
    plugin_dll: Path | None = None,
    plugin_deps: Path | None = None,
    aps_client_id: str | None = None,
    aps_client_secret: str | None = None,
) -> None:
    cid, sec = load_aps_credentials(
        aps_path,
        fallback_client_id=aps_client_id,
        fallback_client_secret=aps_client_secret,
    )
    token = get_da_token(cid, sec)
    bkey = bucket_key or f"da-{uuid.uuid4().hex[:20]}"
    ensure_bucket(token, bkey)

    bundle_dir = _default_bundle_contents_dir()
    dll_path = plugin_dll or (bundle_dir / "LayerPdfExport.dll")
    deps_path = plugin_deps or (bundle_dir / "LayerPdfExport.deps.json")
    if not dll_path.is_file():
        raise FileNotFoundError(
            f"LayerPdfExport.dll not found: {dll_path} "
            "(build the bundle or pass --plugin-dll)"
        )
    if not deps_path.is_file():
        raise FileNotFoundError(
            f"LayerPdfExport.deps.json not found: {deps_path} "
            "(build the bundle or pass --plugin-deps)"
        )

    object_name = dwg_path.name
    host_urn = upload_object(token, bkey, object_name, dwg_path)
    dll_urn = upload_object(token, bkey, "workitem/LayerPdfExport.dll", dll_path)
    deps_urn = upload_object(token, bkey, "workitem/LayerPdfExport.deps.json", deps_path)

    out_name = f"{Path(object_name).stem}_layer_pdfs.zip"
    put_url, upload_key, post_base = prepare_put_url_for_new_object(token, bkey, out_name)

    wid = create_workitem(
        token,
        activity_id,
        host_urn,
        dll_urn,
        deps_urn,
        put_url,
        host_is_oss_urn=True,
        output_is_presigned_s3_put=True,
    )
    result = poll_workitem(token, wid)
    if (result.get("status") or "").lower() != "success":
        rep = result.get("reportUrl", "")
        if rep:
            try:
                rr = requests.get(rep, timeout=120)
                if rr.ok:
                    LOG.error("WorkItem report (truncated):\n%s", rr.text[:12000])
            except OSError as ex:
                LOG.error("Could not fetch report: %s", ex)
        msg = f"WorkItem failed: {result}\nReport: {rep}"
        raise RuntimeError(msg)

    complete_signed_upload(token, post_base, upload_key)
    data = download_object_bytes(token, bkey, out_name)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    out_zip.write_bytes(data)
    LOG.info("Saved %s (%d bytes)", out_zip, len(data))


def main() -> int:
    p = argparse.ArgumentParser(
        description="Design Automation: LayerPdfExport WorkItem (see design_automation/README_SETUP.txt)."
    )
    p.add_argument("--input", type=Path, required=True, help="Input .dwg")
    p.add_argument("--output", type=Path, required=True, help="Output .zip path (layer_pdfs.zip inside)")
    p.add_argument("--aps", type=Path, default=Path(".aps"))
    p.add_argument(
        "--activity-id",
        default=None,
        help="Full Design Automation activity id (else DA_ACTIVITY_ID env, else DEFAULT_DA_ACTIVITY_ID)",
    )
    p.add_argument("--bucket-key", default=None, help="Optional OSS bucket key")
    p.add_argument(
        "--plugin-dll",
        type=Path,
        default=None,
        help="LayerPdfExport.dll (default: design_automation/.../Contents/LayerPdfExport.dll)",
    )
    p.add_argument(
        "--plugin-deps",
        type=Path,
        default=None,
        help="LayerPdfExport.deps.json (default: alongside plugin DLL)",
    )
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    act = (
        args.activity_id
        or os.environ.get("DA_ACTIVITY_ID", "").strip()
        or DEFAULT_DA_ACTIVITY_ID.strip()
    )
    if not act:
        LOG.error(
            "Set --activity-id, DA_ACTIVITY_ID, or DEFAULT_DA_ACTIVITY_ID in da_layer_pdf_pipeline.py."
        )
        return 1

    inp = args.input.expanduser().resolve()
    if not inp.is_file():
        LOG.error("Input not found: %s", inp)
        return 1
    out = args.output.expanduser().resolve()
    try:
        run_pipeline(
            inp,
            out,
            args.aps.expanduser().resolve(),
            act,
            args.bucket_key,
            plugin_dll=args.plugin_dll.expanduser().resolve() if args.plugin_dll else None,
            plugin_deps=args.plugin_deps.expanduser().resolve() if args.plugin_deps else None,
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
