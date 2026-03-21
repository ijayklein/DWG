"""
FastAPI service: upload a DWG → Design Automation (LayerPdfExport) → download ``layer_pdfs.zip``.

Configure via environment (see ``config.py`` and Railway variables).
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from webapp import config

# Repo root on PYTHONPATH
from da_layer_pdf_pipeline import run_pipeline

LOG = logging.getLogger("webapp")

app = FastAPI(
    title="DWG → layout PDFs",
    description=(
        "Upload a `.dwg` file. Returns a zip of PDFs (one per paper layout) produced by "
        "Autodesk Design Automation using the LayerPdfExport bundle."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    """Fail if required config or plugin files are missing (for load balancers)."""
    errors: list[str] = []
    if not config.DA_ACTIVITY_ID:
        errors.append("DA_ACTIVITY_ID is not set")
    if not config.aps_credentials_configured():
        errors.append(
            "APS credentials missing: set APS_CLIENT_ID + APS_CLIENT_SECRET, or "
            "PRECONFIGURED_APS_CLIENT_ID + PRECONFIGURED_APS_CLIENT_SECRET in webapp/config.py, "
            "or provide a readable APS_CREDENTIALS_PATH / .aps file",
        )
    dll = config.PLUGIN_BUNDLE_DIR / "LayerPdfExport.dll"
    deps = config.PLUGIN_BUNDLE_DIR / "LayerPdfExport.deps.json"
    if not dll.is_file():
        errors.append(f"Missing plugin DLL: {dll}")
    if not deps.is_file():
        errors.append(f"Missing plugin deps: {deps}")
    if errors:
        return JSONResponse(status_code=503, content={"status": "not_ready", "errors": errors})
    return JSONResponse(content={"status": "ready"})


@app.post("/api/v1/convert")
async def convert_dwg(
    file: UploadFile = File(..., description="Autodesk DWG drawing"),
) -> Response:
    if not config.DA_ACTIVITY_ID:
        raise HTTPException(
            status_code=503,
            detail="Server misconfiguration: set DA_ACTIVITY_ID.",
        )

    name = file.filename or "upload.dwg"
    if not name.lower().endswith(".dwg"):
        raise HTTPException(
            status_code=400,
            detail="Expected a .dwg file.",
        )

    raw = await file.read()
    if len(raw) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {config.MAX_UPLOAD_BYTES} bytes).",
        )
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="Empty file.")

    job = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix=f"dwg_{job}_") as tmp:
        dwg_path = Path(tmp) / name
        out_zip = Path(tmp) / "layer_pdfs.zip"
        dwg_path.write_bytes(raw)

        try:
            run_pipeline(
                dwg_path,
                out_zip,
                config.APS_CREDENTIALS_PATH,
                config.DA_ACTIVITY_ID,
                config.DA_BUCKET_KEY,
                plugin_dll=config.PLUGIN_BUNDLE_DIR / "LayerPdfExport.dll",
                plugin_deps=config.PLUGIN_BUNDLE_DIR / "LayerPdfExport.deps.json",
                aps_client_id=config.PRECONFIGURED_APS_CLIENT_ID or None,
                aps_client_secret=config.PRECONFIGURED_APS_CLIENT_SECRET or None,
            )
        except FileNotFoundError as e:
            LOG.exception("Pipeline file error")
            raise HTTPException(status_code=503, detail=str(e)) from e
        except Exception as e:
            LOG.exception("Design Automation failed")
            raise HTTPException(
                status_code=502,
                detail=f"Conversion failed: {e!s}"[:2000],
            ) from e

        data = out_zip.read_bytes()

    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(name).stem)[:80]
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}_layer_pdfs.zip"',
        },
    )


def create_app() -> FastAPI:
    return app
