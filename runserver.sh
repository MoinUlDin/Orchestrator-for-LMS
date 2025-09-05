#!/usr/bin/env bash
set -euo pipefail

# default DJANGO_SETTINGS_MODULE if not set - adjust if your project module differs
: "${DJANGO_SETTINGS_MODULE:=orchestrator.settings}"
export DJANGO_SETTINGS_MODULE

echo ">>> Starting orchestrator container at $(date) <<<"
echo "DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}"

# Basic required env checks (fail fast in production if KEY missing)
if [ "${DJANGO_SETTINGS_MODULE}" = "orchestrator.settings" ]; then
  echo "Using default DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}"
fi

# Migration retry parameters (can be overridden via env)
MAX_TRIES=${DB_WAIT_MAX_TRIES:-6}
TRY=0
until python manage.py migrate --noinput; do
  TRY=$((TRY+1))
  if [ "${TRY}" -ge "${MAX_TRIES}" ]; then
    echo "Migrations failed after ${TRY} attempts; continuing to start (you may want to fail instead)." >&2
    break
  fi
  WAIT_SEC=$(( 2 ** (TRY - 1) ))
  echo "Waiting ${WAIT_SEC}s for DB (attempt ${TRY}/${MAX_TRIES})..."
  sleep "${WAIT_SEC}"
done

echo "Collecting static files..."
python manage.py collectstatic --noinput || true

# Start Gunicorn
GUNICORN_WORKERS=${GUNICORN_WORKERS:-3}
GUNICORN_THREADS=${GUNICORN_THREADS:-2}
GUNICORN_TIMEOUT=${GUNICORN_TIMEOUT:-120}

echo "Starting Gunicorn on 0.0.0.0:80 (workers=${GUNICORN_WORKERS})"
exec gunicorn orchestrator.wsgi:application \
  --bind 0.0.0.0:80 \
  --workers "${GUNICORN_WORKERS}" \
  --threads "${GUNICORN_THREADS}" \
  --timeout "${GUNICORN_TIMEOUT}" \
  --log-level info
