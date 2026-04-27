#!/usr/bin/env bash
set -e
echo "==> Installing Python dependencies..."
pip install -r requirements.txt
echo "==> Installing Playwright Chromium..."
playwright install chromium
playwright install-deps chromium
echo "==> Done!"