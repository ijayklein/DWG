Layer PDF export — complete path (Design Automation + this repo)
================================================================

What you get
------------
1. design_automation/LayerPdfExport/ — C# plugin (AutoCAD 2026 API via NuGet) that runs
   command ExportLayerPdfs: for each layer, turns other layers off, exports PDF, zips to
   layer_pdfs.zip in the job working folder.
2. .github/workflows/build-layer-pdf-bundle.yml — builds on GitHub’s Windows runner (no Mac
   required). Download the “LayerPdfExport_bundle” artifact (ZIP of LayerPdfExport.bundle).
3. da_layer_pdf_pipeline.py — uploads your DWG to OSS, runs a WorkItem, downloads the zip.

You still must register the AppBundle + Activity once in your APS account. Use
``da_register_batch.py`` (repo root, same ``.aps`` as other scripts) or the manual
``CREATE_ACTIVITY_STEPS.txt`` curl flow.


GitHub website — download the bundle artifact (step by step)
--------------------------------------------------------------
1. In a browser, open your repo: https://github.com/<your-account>/<repo>  (e.g. the DWG repo).

2. Click the **Actions** tab at the top. You see a list of **workflow runs** (each row is one
   time a workflow ran).

3. In the left sidebar, click **“Build LayerPdfExport bundle”** (the name comes from
   ``.github/workflows/build-layer-pdf-bundle.yml``). The center panel then shows only runs
   of that workflow.

4. Start a run if you need to:
   • **Manual run:** open **“Build LayerPdfExport bundle”**, click **“Run workflow”** (right
     side), choose branch **main**, then **“Run workflow”** again. A new run appears at the top.
   • **Automatic run:** any push that changes files under ``design_automation/LayerPdfExport/``
     also starts this workflow.

5. Wait until the latest run shows a **green checkmark** (success). Click that run’s title
   row to open the **run detail** page.

6. Scroll to the bottom of the run page to the **Artifacts** section.

7. Click **LayerPdfExport_bundle** to download. Your browser saves a zip; that file is the
   bundle package used with ``da_register_batch.py --bundle-zip …`` (often named
   ``LayerPdfExport_bundle.zip`` after download).


Step A — Build the bundle (pick one)
--------------------------------------
A1) GitHub: use the steps in “GitHub website — download the bundle artifact” above, or
    ``gh workflow run`` / ``gh run download`` from a machine with the GitHub CLI.
A2) Windows with .NET 8 SDK:  
    dotnet build design_automation/LayerPdfExport/LayerPdfExport.csproj -c Release  
    The bundle folder is design_automation/LayerPdfExport/LayerPdfExport.bundle/


Step B — Zip the bundle for upload
-----------------------------------
The ZIP root must contain **PackageContents.xml** (not a single folder ``LayerPdfExport.bundle/``
above it), or Design Automation fails: “package has no PackageContents.xml”. From
``design_automation/LayerPdfExport``:

  cd LayerPdfExport.bundle
  Compress-Archive -Path * -DestinationPath ..\LayerPdfExport_bundle.zip -Force


Step C — Register AppBundle + Activity (one-time)
-------------------------------------------------
Use the official tutorial “Upload AppBundle” / “CreateActivity” for AutoCAD, or aps-da-cli.

Engine: pick an AutoCAD engine that matches the API generation you built against (this project
uses Autodesk AutoCAD.NET 25.x NuGet → align with engine **Autodesk.AutoCAD+25_1** or the
closest listed in GET …/da/us-east/v3/engines).

Your Activity commandLine must launch accoreconsole, load this bundle, and run run.scr (which
calls ExportLayerPdfs). Copy the pattern from Autodesk’s “UpdateDWGParam” tutorial and replace
command names / bundle id with LayerPdfExport.

WorkItem arguments used by da_layer_pdf_pipeline.py (must match your Activity definition):
  HostDwg   — GET  — input drawing (urn + Bearer)
  ResultZip — PUT  — output zip (pre-signed S3 URL + Bearer)


Step D — Run the pipeline
-------------------------
Set environment variable (or pass flag):

  export DA_ACTIVITY_ID='YourAPSId.LayerPdfExportActivity+prod'

Run:

  .venv/bin/python da_layer_pdf_pipeline.py --input example.dwg --output ./layer_pdfs.zip --aps .aps

Same .aps client_id:secret as Model Derivative. OAuth scopes must include code:all (already
requested by the script).


Troubleshooting
---------------
• WorkItem fails: open reportUrl from the error JSON; fix Activity commandLine or plugin.
• Empty PDFs: EXPORT prompts differ by drawing; you may need PlotEngine code instead of -EXPORT.
• Engine mismatch: rebuild against the NuGet version that matches the cloud engine, or change engine.
