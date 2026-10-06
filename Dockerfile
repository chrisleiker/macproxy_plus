FROM python:3.11-slim

# git: install pillow-svg from GitHub; libegl1/libgl1/libfontconfig1/libglib2.0-0: runtime deps of skia (via pillow-svg)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libegl1 libgl1 libfontconfig1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Base requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Headless Chromium for RENDER_JAVASCRIPT (adds roughly 500MB; unused unless that setting is on)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Requirements for every extension, so any of them can be enabled via config.py
# without rebuilding the image
COPY extensions/ extensions/
RUN for f in extensions/*/requirements.txt; do \
        [ -e "$f" ] && pip install --no-cache-dir -r "$f"; \
    done; true

COPY . .
RUN chmod +x docker-entrypoint.sh

EXPOSE 5001
ENTRYPOINT ["./docker-entrypoint.sh"]
