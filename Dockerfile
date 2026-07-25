# ── Stage 1: Base ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

WORKDIR /app

# Install only production dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: App ───────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copy production dependencies from builder
COPY --from=base /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=base /usr/local/bin /usr/local/bin

# Copy application code
COPY webhook_server.py .
COPY coolify_deployment_logs.py .
COPY github_issue_creator.py .

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run as non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

CMD ["python", "webhook_server.py"]
