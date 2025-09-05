#!/usr/bin/env bash
set -euo pipefail

# Adjust as needed — replace <your_project_name> with your Django project package
: "${DJANGO_SETTINGS_MODULE:=<your_project_name>.settings}"
export DJANGO_SETTINGS_MODULE

# Optional: nice debugging
echo "Starting container with DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE}"
echo "Collecting static, running migrations, then starting gunicorn on :80"

# Wait for Postgres to be available (exponential backoff)
# Uses manage.py migrate for the check (requires installed psycopg2)
MAX_TRIES=${DB_WAIT_MAX_TRIES:-6}
TRY=0
until python manage.py migrate --noinput; do
  TRY=$((TRY+1))
  if [ "${TRY}" -ge "${MAX_TRIES}" ]; then
    echo "Migrations failed after ${TRY} attempts, continuing to start (you can choose to abort)." >&2
    break
  fi
  WAIT_SEC=$(( 2 ** (TRY - 1) ))
  echo "Waiting ${WAIT_SEC}s for DB (attempt ${TRY}/${MAX_TRIES})..."
  sleep "${WAIT_SEC}"
done

# Collect static files (no input)
python manage.py collectstatic --noinput || true

# Start gunicorn
GUNICORN_WORKERS=${GUNICORN_WORKERS:-3}
GUNICORN_THREADS=${GUNICORN_THREADS:-2}
GUNICORN_TIMEOUT=${GUNICORN_TIMEOUT:-120}

exec gunicorn "<your_project_name>.wsgi:application" \
  --bind 0.0.0.0:80 \
  --workers "${GUNICORN_WORKERS}" \
  --threads "${GUNICORN_THREADS}" \
  --timeout "${GUNICORN_TIMEOUT}" \
  --log-level info
