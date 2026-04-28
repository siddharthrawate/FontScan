#!/usr/bin/env bash
set -e

echo "==> Installing Python dependencies..."
pip install -r requirements.txt

echo "==> Installing Playwright Chromium to project directory..."
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/src/.playwright-browsers
playwright install chromium
playwright install-deps chromium || true

echo "==> Verifying Chromium binary..."
find /opt/render/project/src/.playwright-browsers -name "chrome" -o -name "chromium" -o -name "chrome-headless-shell" 2>/dev/null | head -5
echo "==> Build done!"