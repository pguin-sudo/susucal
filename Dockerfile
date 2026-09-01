FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SUSUCAL_CONFIG=/config/settings.yml \
    SUSUCAL_STATE_DIR=/state

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && \
    useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin susucal && \
    mkdir -p /config /state && chown -R susucal /state

USER susucal
VOLUME ["/config", "/state"]

ENTRYPOINT ["susucal"]
