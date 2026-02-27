#!/bin/bash
set -e
echo "======================================"
echo "Starting Application"
echo "======================================"
echo "PORT value: ${PORT}"
echo "DATABASE_URL set: $( [ -n "${DATABASE_URL}" ] && echo 'YES' || echo 'NO' )"
echo "======================================"

APP_PORT="${PORT:-8000}"
echo "Using port: ${APP_PORT}"
echo "Starting gunicorn..."

exec gunicorn run:app \
    --bind "0.0.0.0:${APP_PORT}" \
    --workers 2 \
    --threads 2 \
    --timeout 120 \
    --log-level debug \
    --access-logfile - \
    --error-logfile -
