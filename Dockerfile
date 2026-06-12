# ── Lead Automation — Production Image ───────────────────────────────
# Builds a self-contained image with Chromium (Playwright) included.
# Credentials, .env, logs, and screenshots are mounted at runtime via
# docker-compose.yml — nothing sensitive is baked into the image.

# NOTE: pinned to -bookworm (Debian 12). Plain python:3.11-slim now resolves to
# Debian 13 (Trixie), which Playwright 1.52 doesn't support — `playwright
# install --with-deps` fails on it (missing ttf-unifont / ttf-ubuntu-font-family).
FROM python:3.11-slim-bookworm

WORKDIR /app

# Install Playwright's system-level Chromium dependencies
# (playwright install --with-deps handles the rest)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer cached separately from app code)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install chromium --with-deps

# Copy application code (credentials/, .env, logs/, screenshots/ are
# excluded by .dockerignore and volume-mounted at runtime)
COPY . .

# Ensure runtime directories exist even before volumes are mounted
RUN mkdir -p logs screenshots credentials

# Playwright runs as root in Docker and auto-adds --no-sandbox
ENV HEADLESS=true \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "main.py"]
