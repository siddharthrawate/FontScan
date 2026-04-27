#!/usr/bin/env bash
set -e

echo "==> Installing Google Chrome..."
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
apt-get update -qq
apt-get install -y -qq google-chrome-stable libgbm1 libasound2
echo "==> Chrome installed: $(google-chrome-stable --version)"

echo "==> Installing Python dependencies..."
pip install -r requirements.txt
