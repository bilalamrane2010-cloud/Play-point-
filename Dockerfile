FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV HEADLESS=1
ENV TZ=Africa/Algiers
ENV PYTHONUNBUFFERED=1
ENV BOT_DATA_DIR=/app/bot_data

# Debian bookworm packages. NOTE: the correct package name on bookworm is
# "libasound2" (the "libasound2t64" name only exists on Ubuntu 24.04 / trixie+).
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    fonts-noto-color-emoji \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    ca-certificates \
    procps \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Persist accounts/settings/scheduler state outside the container layer.
# NOTE: the Dockerfile "VOLUME" instruction is NOT supported by Railway
# (and is ignored by most PaaS builders). Mount a Railway Volume at
# /app/bot_data instead, or point BOT_DATA_DIR at your mounted path.
RUN mkdir -p /app/bot_data

CMD ["python", "main.py"]
