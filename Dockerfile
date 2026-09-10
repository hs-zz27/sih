# SUTRA Workbench - application image.
#
# Two things this image must get right, both of them sovereignty properties:
#
#   1. Everything is installed at BUILD time. Nothing is fetched when it runs,
#      so a cold start with the network unplugged behaves exactly like a warm
#      one. The offline env vars below are belt and braces on top of that.
#   2. It contains no model weights. Ollama serves those from its own container
#      and its own volume, so this image stays small and the licence story stays
#      simple - we ship code, not somebody's weights.

FROM python:3.12-slim

# tesseract: OCR for scanned inspection reports (H1).
# lsof:      the out-of-process egress observer (H4) shells out to it.
# poppler:   PDF rasterisation used by the ingestion pipeline.
RUN apt-get update && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        tesseract-ocr-eng \
        lsof \
        poppler-utils \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a source edit does not re-install 2 GB of wheels.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Embedding weights are baked in at build time. Without this the first search
# would try to reach HuggingFace - which is exactly the failure the whole
# submission is about.
COPY scripts/fetch_models.py scripts/
COPY config.yaml .
COPY src/ src/
RUN python scripts/fetch_models.py

COPY . .

# Nothing may reach for the network at runtime.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Reports the engine, the index and whether the model server is reachable.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
