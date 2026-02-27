# Use a standard Python base image with all system deps for Playwright
FROM python:3.11-slim-bookworm

WORKDIR /app

# Install system dependencies required by Playwright browsers
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxshmfence1 libx11-xcb1 \
    libxcb1 libxext6 libx11-6 libxcb-dri3-0 libxfixes3 \
    fonts-liberation fonts-noto-color-emoji xvfb \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install ALL Playwright browser binaries + their OS-level dependencies
# This ensures chromium, chromium_headless_shell, ffmpeg etc. are all present
RUN playwright install --with-deps

# Copy the app code
COPY . .

# Prevent Python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["python", "server.py"]
