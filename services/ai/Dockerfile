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

# No compiler: every pinned dependency ships a manylinux wheel for CPython 3.12,
# including pyahocorasick's C extension. Installing build-essential "just in
# case" cost 338MB of image for nothing. If a future dependency needs to build
# from source, add a builder stage and copy the wheel forward rather than
# shipping a toolchain to production.
# curl is kept because the HEALTHCHECK uses it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
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

# Package set driven by the actual resume template, not guessed:
#   latex-base/recommended  geometry, fontenc, inputenc, lmodern, babel
#   latex-extra             titlesec, enumitem, fancyhdr, tabularx, xcolor
#   fonts-extra             fontawesome5  <-- ~1GB, and unavoidable: the
#                           template's \faIcon links depend on it, and a
#                           missing style file is a hard compile failure.
# See docs/09-challenges.md for the size/compatibility trade-off.
RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-latex-base \
        texlive-latex-recommended \
        texlive-fonts-recommended \
        texlive-latex-extra \
        texlive-fonts-extra \
    && rm -rf /var/lib/apt/lists/*

# Chromium goes to a fixed, world-readable path rather than the installing
# user's ~/.cache. The install runs as root but the service runs as appuser, and
# a per-user cache path means the browser is simply not found at runtime --
# Playwright then prints "please run playwright install" from inside the image
# that supposedly has it. --with-deps pulls the shared libraries Debian slim lacks.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

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


# ─────────────────────────────────────────────────────────────────────
# Integration test target: the real runtime image plus test tooling.
#
# Exists because the production image deliberately has no pytest, and the
# fast `test` image deliberately has no Chromium or TeX Live -- so neither
# can verify that the browser and pdflatex actually work *as the non-root
# service user*. That gap hid a real bug: Chromium installed into root's
# cache while the service ran as appuser.
# ─────────────────────────────────────────────────────────────────────
FROM runtime AS runtime-test

USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
USER appuser

CMD ["pytest", "tests/", "-v"]
