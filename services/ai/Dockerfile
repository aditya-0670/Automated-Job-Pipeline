# syntax=docker/dockerfile:1

# ─────────────────────────────────────────────────────────────────────
# Base: shared Python layer. Pinned to 3.12 because pyahocorasick and
# yake have no wheels for 3.14 -- the container is the reason the build
# is reproducible regardless of the developer's local interpreter.
# ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# build-essential is needed to compile pyahocorasick's C extension.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ─────────────────────────────────────────────────────────────────────
# Test target: dev dependencies, no browser or LaTeX payload. Used by CI
# for the unit-test job so test feedback does not wait on a 2GB install.
# ─────────────────────────────────────────────────────────────────────
FROM base AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY . .
CMD ["pytest", "-v"]


# ─────────────────────────────────────────────────────────────────────
# Runtime: adds Playwright's Chromium (Tier-2 scraping fallback) and a
# minimal TeX Live for LaTeX -> PDF compilation.
# ─────────────────────────────────────────────────────────────────────
FROM base AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-latex-base \
        texlive-latex-recommended \
        texlive-fonts-recommended \
        texlive-latex-extra \
    && rm -rf /var/lib/apt/lists/*

# --with-deps pulls the shared libraries Chromium needs on Debian slim.
RUN playwright install --with-deps chromium

COPY . .

# Run as non-root: LaTeX compiles untrusted user input, so the process
# should not own the filesystem it writes to.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/out \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
