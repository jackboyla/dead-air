# Image for the measurement sidecars (tap, exporter). Deliberately small: no CUDA,
# no torch, no model weights. The speech pipeline is a separate, much larger image
# and is not built here.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency metadata first so the layer caches across source edits.
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

# Runs unprivileged; nothing here needs root.
RUN useradd --create-home --uid 10001 deadair \
    && mkdir -p /traces && chown deadair:deadair /traces
USER deadair

ENTRYPOINT ["deadair"]
CMD ["--help"]
