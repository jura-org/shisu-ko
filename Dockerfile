# Shisu-ko transcription server image
#
# Build:  docker compose build          (or: docker build -t shisu-ko-server .)
# Run:    docker compose up -d          (GPU)   /   docker compose -f compose.cpu.yaml up -d   (CPU)
#
# Models, cached audio and cue files live in the /data volume (SHISUKO_HOME), so they survive
# image rebuilds and can be shared with the native setup in ~/.shisu-ko.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SHISUKO_HOME=/data \
    # server.py never takes a browser for YouTube's cookies from config.json in here: a DATA_DIR
    # shared with the native setup may name one, and the container has no browser to read.
    SHISUKO_CONTAINER=1 \
    HF_HUB_DISABLE_XET=1 \
    # CUDA runtime libraries come from the nvidia-* pip wheels; the GPU driver comes from the host.
    LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib

# Deno is yt-dlp's preferred JavaScript runtime for solving YouTube's player challenges.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/requirements.txt ./
RUN pip install -r requirements.txt \
    && pip install nvidia-cublas-cu12 nvidia-cudnn-cu12

COPY server/server.py ./

VOLUME ["/data"]
EXPOSE 8790

# The first start downloads the model (about 3 GB for large-v3), so give the health check time.
HEALTHCHECK --interval=30s --timeout=6s --start-period=20m --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=5).status == 200 else 1)"

ENTRYPOINT ["python", "server.py", "--host", "0.0.0.0", "--port", "8790"]
CMD ["--model", "large-v3"]
