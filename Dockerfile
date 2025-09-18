# Use Python 3.11 slim image
FROM python:3.11-slim

# Install system dependencies for Chrome and GUI
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    xvfb \
    x11vnc \
    fluxbox \
    wmctrl \
    && rm -rf /var/lib/apt/lists/*


# Install Google Chrome (modern approach)
RUN mkdir -p /etc/apt/keyrings \
  && wget -O /etc/apt/keyrings/google-chrome.gpg https://dl.google.com/linux/linux_signing_key.pub \
  && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list \
  && apt-get update \
  && apt-get install -y google-chrome-stable \
  && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy application code
COPY . .

# Create a script to start the virtual display and app
RUN echo '#!/bin/bash\n\
# Start virtual display\n\
Xvfb :99 -screen 0 1024x768x24 &\n\
export DISPLAY=:99\n\
# Start window manager\n\
fluxbox &\n\
# Start the Flask app\n\
python app.py' > start.sh && chmod +x start.sh

# Expose port
EXPOSE 10000

# Set environment variables
ENV DISPLAY=:99
ENV FLASK_ENV=production
ENV PORT=10000

# Start the application
CMD ["./start.sh"]
