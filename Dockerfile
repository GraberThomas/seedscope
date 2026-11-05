FROM python:3.13-alpine

RUN apk add --no-cache tzdata \
    && addgroup -g 10001 seedscope \
    && adduser -D -H -u 10001 -G seedscope seedscope \
    && mkdir /data \
    && chown seedscope:seedscope /data

WORKDIR /app
COPY seedscope ./seedscope

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2

USER seedscope
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

CMD ["python", "-m", "seedscope"]
