# EbeyParser for a home server, 24/7. Multi-arch: linux/amd64 and linux/arm64 (Raspberry Pi 5,
# N100 mini-PC, old laptop) — the base image and every dependency ship wheels for both.
#   docker compose up -d        (see docker-compose.yml and README → «Установка на домашний сервер»)
# Everything the program keeps (config.yaml, .env with keys, database, log) is in /app/data (a volume).
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    TZ=Europe/Berlin \
    MALLOC_ARENA_MAX=2 \
    EBEYPARSER_NO_BROWSER=1 \
    EBEYPARSER_CONFIG=/app/data/config.yaml

RUN groupadd --gid 1000 ebey \
    && useradd --uid 1000 --gid ebey --home-dir /app --no-create-home --shell /usr/sbin/nologin ebey

WORKDIR /app

# Dependencies first (from pyproject.toml): a code update doesn't download them again.
# EXTRAS: optional groups from pyproject.toml ("claude" = Claude as a second opinion; "" = none).
ARG EXTRAS=claude
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
    && python -c "import os, tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
extras = [d for e in os.environ.get('EXTRAS', '').split(',') if e.strip() for d in p['optional-dependencies'][e.strip()]]; \
print('\n'.join(p['dependencies'] + extras))" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# The program itself runs from /app (config.example.yaml next to it seeds the first config.yaml).
COPY README.md config.example.yaml .env.example ./
COPY ebeyparser ./ebeyparser
COPY deploy/healthcheck.py ./deploy/healthcheck.py
RUN mkdir -p /app/data && chown ebey:ebey /app/data

USER ebey
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=3 \
    CMD ["python", "/app/deploy/healthcheck.py"]

# --server: no browser; the first start opens the panel to the home network (with a key) and
# prints «Открой http://<IP-сервера>:8000/?token=…» + a QR code: docker compose logs ebeyparser
CMD ["python", "-m", "ebeyparser", "-c", "/app/data/config.yaml", "run", "--server"]
