# Always-on runner: `choices loop` ticks the series every hour.
# docker compose up -d   (see docker-compose.yml)
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config

ENV PYTHONUNBUFFERED=1
VOLUME ["/app/runs"]
HEALTHCHECK --interval=5m --timeout=10s CMD choices status > /dev/null || exit 1
CMD ["choices", "loop", "--interval", "3600"]
