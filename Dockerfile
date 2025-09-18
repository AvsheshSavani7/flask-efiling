FROM python:3.11-slim

# System deps for Playwright browsers and headless
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    unzip \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libxss1 \
    libxtst6 \
    xdg-utils \
    xvfb \
    fluxbox \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's browsers (headless Chromium)
RUN playwright install --with-deps chromium

COPY . .

# Optional: add virtual display/fluxbox if using headed (not headless)
RUN echo '#!/bin/bash\n\
Xvfb :99 -screen 0 1024x768x24 &\n\
export DISPLAY=:99\n\
fluxbox &\n\
python app.py' > start.sh && chmod +x start.sh

EXPOSE 10000
ENV DISPLAY=:99
ENV PORT=10000

CMD ["./start.sh"]
