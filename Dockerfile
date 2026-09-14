# ==============================================================================
# Oracle A1 VM Snatcher - Ultra-lightweight Dockerfile
# Optimized for minimal image size, low memory footprint (~45MB RAM), and fast startup
# ==============================================================================

FROM python:3.12-slim

# Prevent Python from generating .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000 \
    APP_DIR=/app

WORKDIR ${APP_DIR}

# Install essential dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && find /usr/local/lib/python3.12/site-packages -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12/site-packages -name "*.pyc" -delete 2>/dev/null || true

# Copy application files
COPY app.py .
COPY templates/ templates/

# Create a non-privileged user and adjust permissions
RUN groupadd -g 10001 appgroup \
    && useradd -u 10001 -g appgroup -s /bin/sh -M appuser \
    && chown -R appuser:appgroup ${APP_DIR}

USER appuser

EXPOSE 5000

# Health check to ensure service responsiveness
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request, os; port=os.environ.get('PORT', '5000'); urllib.request.urlopen(f'http://localhost:{port}/health')" || exit 1

# Run with Gunicorn using a single process and multi-threading for minimum RAM usage (~45MB)
# and shared in-memory state for the automation loop
CMD gunicorn app:app \
    --bind 0.0.0.0:${PORT:-5000} \
    --workers 1 \
    --threads 4 \
    --worker-class gthread \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
