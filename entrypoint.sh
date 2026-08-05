#!/bin/bash
set -e

# Start the virtual X display that DISPLAY=:99 points at, so headed
# browsers (e.g. the Ohio PUC scraper) can render on a headless server.
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
sleep 1

# Lightweight window manager — some anti-bot sites behave better with one.
fluxbox >/dev/null 2>&1 &

exec "$@"
