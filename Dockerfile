# ---------- builder stage ----------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install build deps required to compile wheels (will be removed in runtime)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gcc \
    curl \
 && rm -rf /var/lib/apt/lists/*

# Copy requirements then build wheels (cached layer)
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel
RUN python -m pip wheel --wheel-dir=/wheels -r /app/requirements.txt

# Copy project sources for any build-time steps
COPY . /app

# ---------- runtime stage ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install only runtime OS deps
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
 && rm -rf /var/lib/apt/lists/*

# Create non-root user and app dir
RUN groupadd --system app && useradd --system --gid app app \
 && mkdir -p /app && chown app:app /app

# Copy pre-built wheels and install
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*

# Copy application code as root (so we can chmod)
COPY . /app

# Ensure runserver.sh is executable, then change ownership
RUN chmod +x /app/runserver.sh \
 && chown -R app:app /app

# Switch to non-root user
USER app

EXPOSE 80

# # Optional small healthcheck - attempts to call /healthz (adjust scheme/port as needed)
# # Note: HEALTHCHECK runs as container user; ensure curl is available (we installed it)
# HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
#   CMD curl -fsS --max-time 5 http://127.0.0.1:80/healthz/ || exit 1

# Entrypoint script (must be executable)
CMD ["./runserver.sh"]
