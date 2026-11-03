# Mock OpenAI-compatible provider for dev, contract and load tests. Build context: repo root.
FROM python:3.14.10-slim-trixie@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY sidecars/mock_llm/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
 && python -m pip uninstall --yes --quiet pip
COPY sidecars/mock_llm/__init__.py sidecars/mock_llm/app.py /app/mock_llm/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status != 200)"]
CMD ["uvicorn", "mock_llm.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
