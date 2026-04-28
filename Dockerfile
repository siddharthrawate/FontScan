FROM python:3.11-slim

# Install system deps + Chrome
RUN apt-get update && apt-get install -y \
    wget gnupg curl unzip \
    libglib2.0-0 libnss3 libfontconfig1 libxss1 libasound2 \
    libatk-bridge2.0-0 libgtk-3-0 libx11-xcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxi6 libxtst6 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libatspi2.0-0 libxext6 libxfixes3 \
    fonts-liberation libu2f-udev libvulkan1 xdg-utils \
    && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*

# Verify Chrome installed
RUN google-chrome-stable --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright and point it at the system Chrome
ENV PLAYWRIGHT_BROWSERS_PATH=/usr/local/lib/playwright-browsers
RUN playwright install chromium --with-deps || true

COPY . .

ENV PORT=10000
ENV GOOGLE_CHROME_BIN=/usr/bin/google-chrome-stable

EXPOSE 10000

CMD uvicorn backend:app --host 0.0.0.0 --port $PORT