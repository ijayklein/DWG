# Layer PDF export (Design Automation, v17 bundle)

This folder is a **self-contained** copy of the scripts and **LayerPdfExport v17** AppBundle needed to run the Autodesk **Design Automation** pipeline locally: upload a **DWG**, run the **LayerPdfExport** activity in the cloud, download a zip of **layout PDFs**.

## What runs

- **`da_layer_pdf_pipeline.py`** — OAuth → OSS upload (DWG + plugin files) → WorkItem → poll → download result.
- **`aps_dwg_convert.py`** — Shared helpers for reading credentials and OSS bucket upload (same as the parent DWG project).

The pipeline targets **region** `us-east` (see `DA_BASE` in `da_layer_pdf_pipeline.py`).

## Credentials

### Option A — `.aps` file (default for this kit)

Create a file named **`.aps`** in **this directory** (`da_layer_pdf_v17/.aps`).

Single line, same format as the rest of the DWG repo:

```text
client_credentials = 'YOUR_CLIENT_ID:YOUR_CLIENT_SECRET'
```

- **Do not commit** `.aps` — it is listed in `.gitignore`.
- Quick copy from the parent project:  
  `cp ../.aps .aps`

### Option B — environment variables

If **`APS_CLIENT_ID`** and **`APS_CLIENT_SECRET`** are set in the environment, they take precedence over the `.aps` file (see `load_aps_credentials` in `da_layer_pdf_pipeline.py`).

### APS app requirements

Your Autodesk Platform Services application must:

- Allow **OAuth2 client credentials** for the IDs above.
- Include scopes needed for **Design Automation** and **OSS** (the pipeline requests: `code:all`, `data:read`, `data:write`, `data:create`, `bucket:create`, `bucket:read`, `bucket:delete`).

## Activity ID (`DA_ACTIVITY_ID`)

The WorkItem must use the **fully qualified** Design Automation activity id:

```text
{NICKNAME}.LayerPdfExportActivity+prod
```

**Default in code:** `da_layer_pdf_pipeline.py` defines **`DEFAULT_DA_ACTIVITY_ID`** so you do not need to `export DA_ACTIVITY_ID` for every run. Override when needed:

1. **Environment:** `export DA_ACTIVITY_ID='YourNick.LayerPdfExportActivity+prod'`
2. **CLI:** `da_layer_pdf_pipeline.py --activity-id '…'`

Edit **`DEFAULT_DA_ACTIVITY_ID`** in `da_layer_pdf_pipeline.py` if this kit is used on another APS app.

To print **your** ids from the parent repo (read-only):  
`../.venv/bin/python ../da_register_batch.py --introspect`  
(requires `../.aps` or the same credentials.)

## Input

| Input | Description |
|--------|-------------|
| **DWG file** | A valid AutoCAD drawing. The sample **`example.dwg`** in this folder is the default input for `run_pipeline.sh`. |
| **AppBundle contents** | Under `design_automation/LayerPdfExport/LayerPdfExport.bundle/` — especially `Contents/LayerPdfExport.dll`, `LayerPdfExport.deps.json`, and `run.scr` (this kit ships the **v17** bundle). |

You can process another drawing:

```bash
.venv/bin/python da_layer_pdf_pipeline.py \
  --input /path/to/your.dwg \
  --output _da_v17_run/layer_pdfs.zip \
  --aps .aps
```

Optional flags (see `--help`): `--activity-id`, `--bucket-key`, `--plugin-dll`, `--plugin-deps`.

## Output

| Output | Description |
|--------|-------------|
| **`layer_pdfs.zip`** | Path is chosen by **`--output`** (e.g. `_da_v17_run/layer_pdfs.zip`). |
| **Inside the zip** | One **PDF per paper layout** produced by the **LayerPdfExport** plugin (naming depends on the plugin; typically one PDF per layout tab). |

If the WorkItem fails, the script exits non‑zero; check the log lines for WorkItem status (`failedInstructions`, `failedUpload`, etc.).

## Quick start

```bash
cd da_layer_pdf_v17
cp ../.aps .aps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run_pipeline.sh
```

Result: **`_da_v17_run/layer_pdfs.zip`** (ignored by git via `.gitignore`).

## Manual command (equivalent to `run_pipeline.sh`)

```bash
mkdir -p _da_v17_run
.venv/bin/python da_layer_pdf_pipeline.py \
  --input example.dwg \
  --output _da_v17_run/layer_pdfs.zip \
  --aps .aps
```

The activity id comes from **`DEFAULT_DA_ACTIVITY_ID`** in `da_layer_pdf_pipeline.py` unless you set **`DA_ACTIVITY_ID`** or **`--activity-id`**.

## Related files in the parent DWG repo

- **`design_automation/README_SETUP.txt`** — Registering AppBundles and activities.
- **`da_register_batch.py`** — Batch register / introspect nickname and qualified ids.
