#!/usr/bin/env python3
"""
Batch-register APS Design Automation resources for **LayerPdfExport**:

1. (Optional) POST **AppBundle** + multipart zip upload + **AppBundle alias** ``prod``
2. POST **Activity** + **Activity alias** ``prod``

Credentials: ``.aps`` (``client_credentials = 'ID:SECRET'``), same as ``aps_dwg_convert``.
OAuth scope **code:all**. No secrets are printed.

``GET …/forgeapps/me`` returns your **nickname** (often your Client ID until you set a friendlier
nickname in the DA console / PATCH forgeapps).

Examples::

  python da_register_batch.py --introspect
  python da_register_batch.py --bundle-zip path/to/LayerPdfExport_bundle.zip
  python da_register_batch.py --skip-appbundle
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import requests

from aps_dwg_convert import load_credentials

LOG = logging.getLogger("da_register_batch")

AUTH_URL = "https://developer.api.autodesk.com/authentication/v2/token"
DA_SCOPES = (
    "code:all data:read data:write data:create bucket:create bucket:read bucket:delete"
)

DEFAULT_REGION = "us-east"
BUNDLE_ID = "LayerPdfExport"
ACTIVITY_ID = "LayerPdfExportActivity"
BUNDLE_ALIAS = "prod"
ACTIVITY_ALIAS = "prod"


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


def da_base(region: str) -> str:
    return f"https://developer.api.autodesk.com/da/{region}/v3"


def get_nickname(token: str, region: str) -> str:
    r = requests.get(
        f"{da_base(region)}/forgeapps/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    r.raise_for_status()
    body = r.json()
    # API may return a JSON string (nickname only) or an object.
    if isinstance(body, str):
        return body
    if isinstance(body, dict) and "nickname" in body:
        return str(body["nickname"])
    raise RuntimeError(f"Unexpected forgeapps/me response: {body!r}")


def list_engine_ids_first_page(token: str, region: str) -> list[str]:
    r = requests.get(
        f"{da_base(region)}/engines",
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    r.raise_for_status()
    data = r.json().get("data") or []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


def pick_autocad_engine(
    engine_ids: list[str],
    override: str | None,
) -> str:
    if override:
        if override not in engine_ids:
            LOG.warning(
                "Engine %s not on first page of /engines; using it anyway (must exist).",
                override,
            )
        return override
    preference = (
        "Autodesk.AutoCAD+25_1",
        "Autodesk.AutoCAD+25_0",
        "Autodesk.AutoCAD+24_3",
        "Autodesk.AutoCAD+24_2",
        "Autodesk.AutoCAD+24_1",
    )
    for p in preference:
        if p in engine_ids:
            return p
    acad = sorted(e for e in engine_ids if e.startswith("Autodesk.AutoCAD+"))
    if not acad:
        raise RuntimeError(
            "No AutoCAD engine id on the first page of GET /engines. "
            "Pass --engine explicitly (see APS GET engines)."
        )
    return acad[-1]


def qualified_appbundle(nickname: str) -> str:
    return f"{nickname}.{BUNDLE_ID}+{BUNDLE_ALIAS}"


def qualified_activity(nickname: str) -> str:
    return f"{nickname}.{ACTIVITY_ID}+{ACTIVITY_ALIAS}"


def activity_body(engine: str, nickname: str) -> dict[str, Any]:
    # Must match design_automation/CREATE_ACTIVITY_STEPS.txt (JSON escaping for accoreconsole).
    return {
        "id": ACTIVITY_ID,
        "commandLine": [
            '$(engine.path)\\\\accoreconsole.exe /i \\"$(args[HostDwg].path)\\" '
            '/al \\"$(appbundles[LayerPdfExport].path)\\" '
            '/s \\"$(appbundles[LayerPdfExport].path)Contents\\\\run.scr\\"'
        ],
        "parameters": {
            "HostDwg": {
                "verb": "get",
                "description": "Input DWG",
                "required": True,
            },
            "ResultZip": {
                "verb": "put",
                "description": "layer_pdfs.zip output",
                "required": True,
                "localName": "layer_pdfs.zip",
            },
        },
        "engine": engine,
        "appbundles": [qualified_appbundle(nickname)],
    }


def post_json(
    token: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    ok: tuple[int, ...] = (200, 201),
) -> requests.Response:
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=payload if payload is not None else {},
        timeout=600,
    )
    if r.status_code not in ok:
        LOG.error("HTTP %s %s\n%s", r.status_code, url, r.text[:4000])
        r.raise_for_status()
    return r


def create_appbundle_and_upload(
    token: str,
    region: str,
    engine: str,
    zip_path: Path,
) -> int:
    """
    Create a new AppBundle (or a new version if the id already exists), upload the zip,
    return the **version** integer to wire the ``prod`` alias to.
    """
    base = da_base(region)
    create_url = f"{base}/appbundles"
    payload = {"id": BUNDLE_ID, "engine": engine}
    r = requests.post(
        create_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=payload,
        timeout=120,
    )
    if r.status_code == 409:
        LOG.info("AppBundle %s exists; creating new version…", BUNDLE_ID)
        r = post_json(token, f"{base}/appbundles/{BUNDLE_ID}/versions", {})
    else:
        r.raise_for_status()

    data = r.json()
    version = int(data.get("version", 1))
    upload = data.get("uploadParameters") or {}
    endpoint = upload.get("endpointURL") or upload.get("endpointUrl")
    form_data = upload.get("formData") or {}
    if not endpoint or not isinstance(form_data, dict):
        raise RuntimeError(f"Unexpected appbundle response (missing uploadParameters): {data!r}")

    # Multipart POST to S3: text fields first, **file** last (per APS docs).
    with zip_path.open("rb") as fh:
        parts: list[tuple[str, Any]] = [(k, str(v)) for k, v in form_data.items()]
        parts.append(
            (
                "file",
                (zip_path.name, fh, "application/zip"),
            )
        )
        up = requests.post(endpoint, files=parts, timeout=600)
    if up.status_code not in (200, 201, 204):
        LOG.error("Package upload failed: %s %s", up.status_code, up.text[:2000])
        up.raise_for_status()

    LOG.info("Uploaded AppBundle package (%s bytes): %s", zip_path.stat().st_size, zip_path)
    return version


def ensure_appbundle_alias(
    token: str,
    region: str,
    version: int,
) -> None:
    url = f"{da_base(region)}/appbundles/{BUNDLE_ID}/aliases"
    post_json(
        token,
        url,
        {"id": BUNDLE_ALIAS, "version": version},
        ok=(200, 201),
    )
    LOG.info("AppBundle alias %s → version %s", BUNDLE_ALIAS, version)


def ensure_activity(token: str, region: str, engine: str, nickname: str) -> int:
    base = da_base(region)
    body = activity_body(engine, nickname)
    r = requests.post(
        f"{base}/activities",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=body,
        timeout=120,
    )
    if r.status_code == 409:
        LOG.info("Activity %s exists; creating new version…", ACTIVITY_ID)
        ver_body = {k: v for k, v in body.items() if k != "id"}
        r = post_json(token, f"{base}/activities/{ACTIVITY_ID}/versions", ver_body)
    else:
        r.raise_for_status()
    data = r.json()
    ver = int(data.get("version", 1))
    LOG.info("Activity version: %s", ver)
    return ver


def ensure_activity_alias(token: str, region: str, version: int) -> None:
    post_json(
        token,
        f"{da_base(region)}/activities/{ACTIVITY_ID}/aliases",
        {"id": ACTIVITY_ALIAS, "version": version},
        ok=(200, 201),
    )
    LOG.info("Activity alias %s → version %s", ACTIVITY_ALIAS, version)


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch DA registration for LayerPdfExport.")
    ap.add_argument(
        "--aps",
        type=Path,
        default=Path(".aps"),
        help="Path to .aps credentials file",
    )
    ap.add_argument("--region", default=DEFAULT_REGION, help="DA region (default us-east)")
    ap.add_argument(
        "--engine",
        default=None,
        help="Force engine id, e.g. Autodesk.AutoCAD+25_1",
    )
    ap.add_argument(
        "--bundle-zip",
        type=Path,
        default=None,
        help="Zip produced by the LayerPdfExport build (GitHub artifact or dotnet publish)",
    )
    ap.add_argument(
        "--skip-appbundle",
        action="store_true",
        help="Do not upload a bundle; only create Activity + aliases (bundle+alias must exist)",
    )
    ap.add_argument(
        "--introspect",
        action="store_true",
        help="Print nickname, chosen engine, and qualified ids; no writes",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    client_id, client_secret = load_credentials(args.aps)
    token = get_da_token(client_id, client_secret)
    nickname = get_nickname(token, args.region)
    engines = list_engine_ids_first_page(token, args.region)
    engine = pick_autocad_engine(engines, args.engine)

    LOG.info("DA nickname: %s", nickname)
    LOG.info("Engine: %s", engine)
    LOG.info("Qualified AppBundle ref: %s", qualified_appbundle(nickname))
    LOG.info("Qualified Activity id: %s", qualified_activity(nickname))

    if args.introspect:
        print(json.dumps(
            {
                "nickname": nickname,
                "engine": engine,
                "appbundle_ref": qualified_appbundle(nickname),
                "activity_id": qualified_activity(nickname),
            },
            indent=2,
        ))
        return 0

    if args.skip_appbundle:
        ver = ensure_activity(token, args.region, engine, nickname)
        ensure_activity_alias(token, args.region, ver)
        print(json.dumps({"activity_id": qualified_activity(nickname)}, indent=2))
        return 0

    if not args.bundle_zip or not args.bundle_zip.is_file():
        LOG.error("Provide --bundle-zip PATH to a built bundle zip, or use --skip-appbundle / --introspect.")
        return 2

    bundle_ver = create_appbundle_and_upload(token, args.region, engine, args.bundle_zip)
    ensure_appbundle_alias(token, args.region, bundle_ver)

    act_ver = ensure_activity(token, args.region, engine, nickname)
    ensure_activity_alias(token, args.region, act_ver)

    out = {
        "appbundle_ref": qualified_appbundle(nickname),
        "activity_id": qualified_activity(nickname),
        "engine": engine,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.HTTPError as e:
        LOG.error("%s", e)
        sys.exit(1)
