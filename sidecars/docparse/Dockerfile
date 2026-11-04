# docparse sidecar (US-15, US-16): MarkItDown for Office files and HTML, PDF text layers, and OCR
# through an OpenAI-compatible vision model (vLLM serving GLM-OCR or PaddleOCR-VL).
# Build context: repo root.
FROM python:3.14.10-slim-trixie@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY sidecars/docparse/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
 && python -m pip uninstall --yes --quiet pip
COPY sidecars/docparse/__init__.py sidecars/docparse/app.py sidecars/docparse/config.py \
     sidecars/docparse/documents.py sidecars/docparse/vision.py /app/docparse/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status != 200)"]
CMD ["uvicorn", "docparse.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
