FROM python:3.11-slim

# Install system dependencies + Google Chrome
RUN apt-get update && apt-get install -y \
    wget gnupg unzip curl xvfb \
    libglib2.0-0 libnss3 libgconf-2-4 libfontconfig1 \
    libxss1 libappindicator3-1 libasound2 libatk-bridge2.0-0 \
    libgtk-3-0 libx11-xcb1 libxcomposite1 libxcursor1 \
    libxdamage1 libxi6 libxtst6 libxrandr2 libpango-1.0-0 \
    libpangocairo-1.0-0 libcairo2 libgbm1 \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Render sets PORT env var; default to 8000
ENV PORT=8000
ENV CHROME_BIN=/usr/bin/google-chrome

EXPOSE 8000

CMD uvicorn backend:app --host 0.0.0.0 --port $PORT
