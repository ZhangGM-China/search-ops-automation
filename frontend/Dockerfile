# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build

ARG NPM_REGISTRY=https://registry.npmmirror.com/

WORKDIR /app

COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --registry="$NPM_REGISTRY" \
    --fetch-timeout=30000 --fetch-retries=1 \
    --prefer-offline --no-audit --no-fund

ARG RELEASE_SHA
ARG SCHEMA_RELEASE
ARG ARTIFACT_MANIFEST_SHA256

COPY tsconfig.json tsconfig.app.json tsconfig.node.json vite.config.ts index.html ./
COPY src ./src
COPY public ./public
RUN npm run build
RUN RELEASE_SHA="$RELEASE_SHA" \
    SCHEMA_RELEASE="$SCHEMA_RELEASE" \
    ARTIFACT_MANIFEST_SHA256="$ARTIFACT_MANIFEST_SHA256" \
    node - <<'JS'
const fs = require("fs");

const releaseSha = process.env.RELEASE_SHA;
const schemaRelease = process.env.SCHEMA_RELEASE;
const artifactManifestSha256 = process.env.ARTIFACT_MANIFEST_SHA256;
if (!/^[0-9a-f]{40}$/.test(releaseSha)) throw new Error("invalid RELEASE_SHA");
if (!schemaRelease.trim() || /[\r\n\t\0]/.test(schemaRelease)) {
  throw new Error("invalid SCHEMA_RELEASE");
}
if (!/^[0-9a-f]{64}$/.test(artifactManifestSha256)) {
  throw new Error("invalid ARTIFACT_MANIFEST_SHA256");
}
fs.writeFileSync(
  "/app/release-info.json",
  `${JSON.stringify({ releaseSha, schemaRelease, artifactManifestSha256 })}\n`,
  "utf8",
);
JS

FROM nginx:alpine@sha256:62ff2089abf5a9ed33bd232895bef5e22f7bb4b200675cec49a5ebc48e3d4ac8

RUN apk del nginx-module-acme nginx-module-geoip nginx-module-image-filter \
        nginx-module-njs nginx-module-xslt expat libexpat curl libcurl && \
    apk add --no-cache --upgrade 'zlib>=1.3.2-r1' 'pcre2>=10.49-r0' && \
    ! apk info -e expat libexpat libxml2 libxslt curl libcurl

ARG RELEASE_SHA
ARG SCHEMA_RELEASE
ARG ARTIFACT_MANIFEST_SHA256

LABEL org.opencontainers.image.revision="$RELEASE_SHA" \
      aiops.schema-release="$SCHEMA_RELEASE" \
      aiops.artifact-manifest-sha256="$ARTIFACT_MANIFEST_SHA256" \
      aiops.component="frontend"

RUN sed -i 's#^[[:space:]]*pid[[:space:]][[:space:]]*/run/nginx.pid;[[:space:]]*$#pid /tmp/nginx.pid;#' /etc/nginx/nginx.conf && \
    sed -i 's#include /etc/nginx/conf.d/\*.conf;#include /tmp/nginx-conf.d/*.conf;#' /etc/nginx/nginx.conf && \
    grep -q 'include /tmp/nginx-conf.d/\*.conf;' /etc/nginx/nginx.conf && \
    rm /etc/nginx/conf.d/default.conf

COPY nginx.conf.template /etc/aiops-nginx/default.conf.template
COPY nginx-entrypoint.sh /usr/local/bin/aiops-nginx-entrypoint
RUN chmod 0555 /usr/local/bin/aiops-nginx-entrypoint
COPY --from=build /app/dist /usr/share/nginx/html
COPY --from=build /app/release-info.json /usr/share/nginx/html/release-info.json

USER 101:101

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/aiops-nginx-entrypoint"]
CMD ["nginx", "-g", "daemon off;"]
