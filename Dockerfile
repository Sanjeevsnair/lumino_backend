# ─────────────────────────────────────────────────────────────────────────────
# Lumino — LookMovie2 API  (test.py)
# FastAPI / uvicorn backend with Cloudflare-bypass stack
#
# Build:   docker build -t lumino-api .
# Run:     docker run -p 7860:7860 lumino-api
#
# Optional env vars (pass with -e or via docker-compose / HF Spaces secrets):
#   FLARESOLVERR_URL   http://flaresolverr-host:8191
#   HTTP_PROXY         http://user:pass@proxy:8080   (or socks5://…)
#   HTTPS_PROXY        same value as HTTP_PROXY
#   RELAY_URL          https://your-worker.your-subdomain.workers.dev
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: base ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

# System dependencies required by curl_cffi (libcurl with TLS) and lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libcurl4-openssl-dev \
        libssl-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: deps ─────────────────────────────────────────────────────────────
FROM base AS deps

WORKDIR /install

# Copy and install Python dependencies first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ── Stage 3: app ──────────────────────────────────────────────────────────────
FROM deps AS app

# Non-root user for security (HF Spaces expects UID 1000)
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Copy only the application file(s) needed to run the API
COPY test.py .

# Ensure the working directory is owned by appuser
RUN chown -R appuser:appuser /app

USER appuser

# ── Runtime ───────────────────────────────────────────────────────────────────

# Port used by uvicorn (matches test.py entrypoint and HF Spaces default)
EXPOSE 7860

# Health-check — polls the /health endpoint every 30 s
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:7860/health || exit 1

# Start the API
CMD ["python", "test.py"]
