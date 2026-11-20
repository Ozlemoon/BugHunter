# BugHunter AI — dashboard + demo targets
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BUGHUNTER_HOST=0.0.0.0

WORKDIR /app
RUN useradd --create-home --uid 10001 bughunter

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=bughunter:bughunter . .
USER bughunter

EXPOSE 8000 8001 8002
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

# 'demo' runs the dashboard and the buggy playground targets. Use 'web' for the dashboard only.
CMD ["python", "main.py", "demo"]
