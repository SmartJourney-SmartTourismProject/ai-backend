# AI backend image (production). Built by CI on every push to main and pushed
# to ghcr.io/smartjourney-smarttourismproject/ai-backend (.github/workflows/ci.yml).
#
# requirements.txt only - NOT requirements-rag.txt: the RAG extras pull in
# sentence-transformers/PyTorch (roughly +2 GB of image). Python 3.12 to match CI.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY main.py ./
COPY app ./app

RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser
EXPOSE 8000

# `GET /` is the service's liveness route. python:slim has no curl.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4).status == 200 else 1)"

# ONE worker on purpose: app/scheduler.py's APScheduler jobs and the LLM-config
# refresh loop (app/core/llm_config.py) start with the app, so a second worker
# would run every data-refresh job twice.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
