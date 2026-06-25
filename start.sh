#!/bin/bash
# SplitEasy – Production startup script
# Uses gunicorn for proper multi-threaded serving

set -e

echo "Starting SplitEasy..."

# Check if gunicorn is available, fall back to Flask dev server
if command -v gunicorn &> /dev/null; then
    echo "Running with gunicorn (production)"
    gunicorn app:app \
        --bind 0.0.0.0:5015 \
        --workers 2 \
        --threads 2 \
        --timeout 60 \
        --access-logfile - \
        --error-logfile -
else
    echo "gunicorn not found, running with Flask dev server"
    echo "Install gunicorn for production: pip install gunicorn"
    python app.py
fi
