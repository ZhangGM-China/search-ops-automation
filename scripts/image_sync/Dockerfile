# Standalone browser bridge image. It is intentionally separate from the
# AIOPS Backend/Scheduler image and has no access to the application API.
FROM node:22-bookworm

ARG DEBIAN_FRONTEND=noninteractive
RUN sed -i -e 's|http://deb.debian.org|https://mirrors.aliyun.com|g' -e 's|http://security.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=20 install --no-install-recommends --yes \
        chromium \
        ca-certificates \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scripts/image_sync/atlas_browser_worker.mjs /app/atlas_browser_worker.mjs

RUN useradd --create-home --uid 10001 worker \
    && mkdir -p /tmp/atlas-browser-profile \
    && chown -R 10001:10001 /app /tmp/atlas-browser-profile

USER 10001:10001
ENV QG_BROWSER_CHROME_PATH=/usr/bin/chromium \
    QG_BROWSER_BIND_HOST=0.0.0.0 \
    QG_BROWSER_HEADLESS=true \
    QG_BROWSER_DISPLAY_MODE=ozone-headless \
    QG_BROWSER_REQUIRE_PROXY=false \
    QG_BROWSER_PROFILE_DIR=/tmp/atlas-browser-profile \
    QG_BROWSER_PORT=8790 \
    QG_BROWSER_CDP_PORT=9231 \
    QG_BROWSER_MIN_INTERVAL_MS=1500 \
    QG_BROWSER_MAX_INTERVAL_MS=2000

EXPOSE 8790
ENTRYPOINT ["node", "/app/atlas_browser_worker.mjs"]
