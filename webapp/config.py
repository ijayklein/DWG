"""Environment-driven settings for Railway and local runs."""

from __future__ import annotations

import os
from pathlib import Path


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser().resolve()


# --- Preconfigured APS app (optional) ---
# If set, used when APS_CLIENT_ID / APS_CLIENT_SECRET are not in the environment.
# Environment variables always override these. Do not commit real secrets to a public repo.
PRECONFIGURED_APS_CLIENT_ID: str = ""
PRECONFIGURED_APS_CLIENT_SECRET: str = ""

# Design Automation Activity (full id ending in +prod or similar)
DA_ACTIVITY_ID: str = os.environ.get("DA_ACTIVITY_ID", "").strip()

# OSS bucket for uploads (optional; auto-generated per job if unset)
DA_BUCKET_KEY: str | None = os.environ.get("DA_BUCKET_KEY", "").strip() or None

# Built LayerPdfExport.bundle/Contents (DLL + deps) — copied into the Docker image
PLUGIN_BUNDLE_DIR: Path = _path(
    "PLUGIN_BUNDLE_DIR",
    str(
        Path(__file__).resolve().parent.parent
        / "design_automation"
        / "LayerPdfExport"
        / "LayerPdfExport.bundle"
        / "Contents"
    ),
)

# Optional .aps path if not using APS_CLIENT_ID / APS_CLIENT_SECRET
APS_CREDENTIALS_PATH: Path = _path("APS_CREDENTIALS_PATH", ".aps")


def aps_credentials_configured() -> bool:
    """True if env, preconfigured constants, or ``APS_CREDENTIALS_PATH`` can supply APS OAuth."""
    if os.environ.get("APS_CLIENT_ID", "").strip() and os.environ.get("APS_CLIENT_SECRET", "").strip():
        return True
    if PRECONFIGURED_APS_CLIENT_ID.strip() and PRECONFIGURED_APS_CLIENT_SECRET.strip():
        return True
    return APS_CREDENTIALS_PATH.is_file()


# Upload limit (bytes)
MAX_UPLOAD_BYTES: int = int(os.environ.get("MAX_UPLOAD_BYTES", str(80 * 1024 * 1024)))  # 80 MiB
