# DWG → layer PDFs API (FastAPI + Design Automation)
# Build the LayerPdfExport .NET bundle first so Contents/ has LayerPdfExport.dll and deps.

FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY aps_dwg_convert.py da_layer_pdf_pipeline.py ./
COPY webapp ./webapp
COPY design_automation/LayerPdfExport/LayerPdfExport.bundle ./design_automation/LayerPdfExport/LayerPdfExport.bundle

# Default plugin path inside the image (override with PLUGIN_BUNDLE_DIR if needed)
ENV PLUGIN_BUNDLE_DIR=/app/design_automation/LayerPdfExport/LayerPdfExport.bundle/Contents

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
