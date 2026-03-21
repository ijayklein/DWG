Layer PDF export — Design Automation (working setup)
======================================================

What works (current method)
----------------------------
The bundle runs **AcCoreConsole** with a small **run.scr** that **NETLOAD**s the plugin DLL from
the job folder, then invokes a C# command.

**Default command: `ExportAllLayoutPdfs`**

• For **every paper layout tab** (everything in the layout dictionary except **Model**), the
  plugin switches to that layout (`LAYOUT` → `S` → layout name), runs `ZOOM` → `E`, then
  **`-EXPORT`** → **PDF** using the **Current layout** option (not Model-space Extents —
  layout tabs use different prompts), then **No** to skip detailed plot UI, then the output path.

• **All CAD layers stay on** for each export. Each layout becomes one PDF named from the layout
  (sanitized filename), e.g. `A1.1a.pdf`, collected under `_layerpdf_out/` and zipped to
  **`layer_pdfs.zip`** in the job folder.

• If the drawing has **no paper layouts**, it exports **Model** once as **`model.pdf`**.

**Why layouts:** The “full page” you see in AutoCAD is usually a **paper layout** (sheet +
  viewports). Exporting **Model** alone often frames only a small crop. Per-layout export matches
  what you see on each layout tab.

**Alternate commands** (change the last line of `Contents/run.scr` or use the matching scr):

  `ExportFlatPdf`       — single PDF for one layout (first non-Model layout, or Model if none).
  `ExportLayerPdfs`     — one PDF per **layer** (isolates layers; uses layout vs model logic).


Repo layout
-----------
• `design_automation/LayerPdfExport/` — C# plugin (AutoCAD.NET 25.x), `LayerPdfExport.bundle/`
  with `PackageContents.xml`, `Contents/run.scr`, built DLL + deps copied on build.

• `.github/workflows/build-layer-pdf-bundle.yml` — Windows CI builds the bundle; artifact
  **LayerPdfExport_bundle** is a zip whose **only file** is **`LayerPdfExport_bundle.zip`**
  (the inner zip is what you register — **PackageContents.xml** must be at the **root** of
  that inner zip).

• `da_register_batch.py` — registers AppBundle + Activity (same `.aps` as other tools).

• `da_layer_pdf_pipeline.py` — uploads DWG + plugin files to OSS, posts WorkItem, downloads
  **`layer_pdfs.zip`**.

**NETLOAD:** AcCoreConsole often does not load the .NET 8 bundle module from `/al` alone.
  The Activity therefore supplies **`LayerPdfExport.dll`** and **`LayerPdfExport.deps.json`**
  into the job folder; **run.scr** NETLOADs the DLL so commands register.


Get the bundle (GitHub)
-------------------------
1. Repo → **Actions** → workflow **Build LayerPdfExport bundle**.
2. Open the latest **green** run → **Artifacts** → download **LayerPdfExport_bundle**.
3. Unzip the outer download; take the inner **`LayerPdfExport_bundle.zip`** and pass that path
   to `da_register_batch.py --bundle-zip` (not the outer wrapper).


Build locally (Windows, optional)
-----------------------------------
  dotnet build design_automation/LayerPdfExport/LayerPdfExport.csproj -c Release

Zip for DA (must have **PackageContents.xml** at zip root):

  cd design_automation/LayerPdfExport/LayerPdfExport.bundle
  Compress-Archive -Path * -DestinationPath ..\LayerPdfExport_bundle.zip -Force


Register once (AppBundle + Activity)
------------------------------------
  python da_register_batch.py --bundle-zip /path/to/LayerPdfExport_bundle.zip

Engine: use an AutoCAD engine that matches the NuGet (e.g. **Autodesk.AutoCAD+25_1**).

Activity **commandLine** pattern (already in `da_register_batch.py`): **accoreconsole** `/al`
bundle, `/i` HostDwg, `/s` … **`Contents\run.scr`**.

WorkItem arguments (must match Activity + `da_layer_pdf_pipeline.py`):

  HostDwg    — GET  — input DWG (OSS URN + Bearer)
  PluginDll  — GET  — `LayerPdfExport.dll` (localName in job folder)
  PluginDeps — GET  — `LayerPdfExport.deps.json`
  ResultZip  — PUT  — `layer_pdfs.zip` (pre-signed S3 PUT URL; no Forge headers on PUT)


Run the pipeline
------------------
  export DA_ACTIVITY_ID='YourNickname.LayerPdfExportActivity+prod'

  python da_layer_pdf_pipeline.py --input your.dwg --output ./layer_pdfs.zip --aps .aps

OAuth scopes must include **code:all** (script already requests them). The pipeline uploads
the DLL and deps from `LayerPdfExport.bundle/Contents/` by default (or `--plugin-dll` /
`--plugin-deps`).


Troubleshooting
---------------
• **failedDownload / failedUpload:** check OSS URIs and signed URLs; HostDwg must be reachable
  by the worker.

• **failedInstructions:** open **reportUrl** from the WorkItem JSON; look for unknown command,
  `-EXPORT` prompt mismatches, or crashes.

• **Wrong PDF framing:** ensure you are using a **layout** export for sheets; for many separate
  floor plans **only in Model** (no extra layout tabs), you need a different pipeline (e.g.
  region detection + separate exports), not one PDF per layout tab.
