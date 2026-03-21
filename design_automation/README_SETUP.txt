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

You still must register the AppBundle + Activity once in your APS account (Autodesk’s
Design Automation console / Postman / aps-da-cli). This cannot be fully automated without
your APS nickname and engine choice.


Step A — Build the bundle (pick one)
--------------------------------------
A1) GitHub: push this repo, open Actions → “Build LayerPdfExport bundle”, download artifact.
A2) Windows with .NET 8 SDK:  
    dotnet build design_automation/LayerPdfExport/LayerPdfExport.csproj -c Release  
    The bundle folder is design_automation/LayerPdfExport/LayerPdfExport.bundle/


Step B — Zip the bundle for upload
-----------------------------------
Zip the *contents* of LayerPdfExport.bundle so the root of the ZIP contains PackageContents.xml
and the Contents folder (same layout as Autodesk samples). Example (PowerShell):

  Compress-Archive -Path .\LayerPdfExport.bundle\* -DestinationPath .\LayerPdfExport.zip


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
