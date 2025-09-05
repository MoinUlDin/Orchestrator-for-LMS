# ---------- builder stage ----------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=off

WORKDIR /app

# Install build deps to build wheels (removed later)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and build wheels
COPY requirements.txt /app/requirements.txt

# Upgrade pip/setuptools to avoid wheel-building issues
RUN python -m pip install --upgrade pip setuptools wheel

# Build wheels into /wheels
RUN python -m pip wheel --wheel-dir=/wheels -r requirements.txt

# Copy project sources
COPY . /app

# ---------- runtime stage ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install only runtime OS deps (minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd --system app && useradd --system --gid app app \
    && mkdir -p /app && chown app:app /app

# Copy wheels from builder and install them
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*

# Copy application code as non-root user
COPY --chown=app:app . /app

USER app

EXPOSE 80

# Entrypoint runs your runserver.sh
CMD ["./runserver.sh"]
