# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
FROM python:3.11-alpine@sha256:cd04730b8511def3fbf14204d66a0c1536f290b8e896ed5a94cd64cb15ac1356

RUN apk upgrade --no-cache
RUN apk add --no-cache --upgrade 'zlib>=1.3.2-r1' && \
    apk add --no-cache --repository https://dl-cdn.alpinelinux.org/alpine/edge/main \
        expat=2.9.0-r0 libexpat=2.9.0-r0

# Alpine stable 尚未提供修复包；仅引入指定的 Expat 包，不升级整个发行版。
# Python 自带的两个 XML 扩展也必须重新链接修复后的库。
RUN <<'SH'
set -eu
test "$PYTHON_VERSION" = '3.11.16'
apk add --no-cache --repositories-file /dev/null \
    --repository https://mirrors.tencent.com/alpine/v3.24/main \
    --virtual .xml-build-deps gcc musl-dev
apk add --no-cache --repository https://dl-cdn.alpinelinux.org/alpine/edge/main \
    --virtual .xml-headers expat-dev=2.9.0-r0
mkdir -p /tmp/python-xml/clinic
python - <<'PY'
import base64
import hashlib
import json
import time
import urllib.request
from pathlib import Path

sources = {
    'pyexpat.c': '3804299cf1b69dcb66d06631c98a2ce40e745ae3194a12e8880990a896ad75d4',
    '_elementtree.c': '3532407351c6bfc6fe5939f5c7ca8e2b88a7e009c3f7c96cb707b99ddf1bb732',
    'clinic/pyexpat.c.h': '85101449af31c3a9749bc596b3b10efd90398c5c3d9e4a587beaa20aadb44b5c',
    'clinic/_elementtree.c.h': '52d3d2d70170cda75e92dc82e4403188f2a5a51eed1cdd443b26f73530b08344',
}
for name, expected in sources.items():
    url = 'https://api.github.com/repos/python/cpython/contents/Modules/' + name + '?ref=v3.11.16'
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                data = base64.b64decode(json.load(response)['content'])
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError('CPython source checksum mismatch: ' + name)
            (Path('/tmp/python-xml') / name).write_bytes(data)
            break
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(2)
PY
for module in pyexpat _elementtree; do
    gcc -shared -fPIC $(python3-config --cflags) \
        -I/usr/local/include/python3.11/internal -I/usr/include \
        /tmp/python-xml/$module.c -lexpat \
        -o /usr/local/lib/python3.11/lib-dynload/$module$(python3-config --extension-suffix)
    strip /usr/local/lib/python3.11/lib-dynload/$module$(python3-config --extension-suffix)
done
apk del .xml-headers .xml-build-deps
rm -rf /tmp/python-xml
python - <<'PY'
import pyexpat
from xml.dom import minidom
from xml.etree import ElementTree
assert pyexpat.version_info >= (2, 9, 0), pyexpat.EXPAT_VERSION
assert ElementTree.fromstring('<root>safe &amp; sound</root>').text == 'safe & sound'
assert minidom.parseString('<root/>').documentElement.tagName == 'root'
PY
SH

WORKDIR /app

ENV TZ=Asia/Shanghai \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/app/data/home \
    XDG_CACHE_HOME=/app/data/cache \
    TMPDIR=/tmp
RUN test -e /usr/share/zoneinfo/$TZ && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
RUN addgroup -S -g 10001 app && \
    adduser -S -D -H -u 10001 -G app app && \
    mkdir -p /app/data/home /app/data/cache && \
    chown -R 10001:10001 /app/data

COPY requirements.lock /app/requirements.lock
ARG PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple
ARG PIP_RETRIES=5
ARG PIP_TIMEOUT=120
RUN --mount=type=cache,target=/app/data/cache/pip \
    pip install --require-hashes -r requirements.lock \
        --retries "$PIP_RETRIES" \
        --timeout "$PIP_TIMEOUT" \
        --index-url "$PIP_INDEX_URL" \
        --prefer-binary \
        --disable-pip-version-check && \
    find /usr/local/lib/python3.11/site-packages -type d \
        \( -name SelfTest -o -name __pycache__ \) -prune -exec rm -rf '{}' + && \
    python -m pip uninstall --yes setuptools wheel && \
    python -m pip uninstall --yes pip

# Fail the image build when the two imports used by production tracing are not
# available.  The application degrades safely at runtime, but a release image
# must not silently ship without its observability runtime.
RUN python - <<'PY'
from langfuse import Langfuse
from langchain_core.callbacks import BaseCallbackHandler
assert Langfuse is not None and BaseCallbackHandler is not None
PY

ARG RELEASE_SHA
ARG SCHEMA_RELEASE
ARG ARTIFACT_MANIFEST_SHA256

LABEL org.opencontainers.image.revision="$RELEASE_SHA" \
      aiops.schema-release="$SCHEMA_RELEASE" \
      aiops.artifact-manifest-sha256="$ARTIFACT_MANIFEST_SHA256" \
      aiops.component="backend"

# PI runs as a separate container from this same immutable release image.
RUN apk add --no-cache nodejs npm
COPY pi-runtime/package.json pi-runtime/package-lock.json /app/pi-runtime/
RUN cd /app/pi-runtime && npm ci --omit=dev --ignore-scripts --no-audit --no-fund && \
    find node_modules -type d \( -name test -o -name tests -o -name coverage \) -prune -exec rm -rf '{}' + && \
    npm cache clean --force && apk del npm
COPY pi-runtime/src /app/pi-runtime/src
RUN cd /app/pi-runtime && node --input-type=module -e "await import('./src/kg-task.mjs')"

COPY main.py /app/main.py
COPY src /app/src
COPY scripts/db/migrate_prod_202607_assistant_top_query.py /app/scripts/db/migrate_prod_202607_assistant_top_query.py
RUN chmod -R a+rX /app/main.py /app/src /app/scripts

RUN RELEASE_SHA="$RELEASE_SHA" \
    SCHEMA_RELEASE="$SCHEMA_RELEASE" \
    ARTIFACT_MANIFEST_SHA256="$ARTIFACT_MANIFEST_SHA256" \
    python - <<'PY'
import json
import os
import re
from pathlib import Path

release_sha = os.environ["RELEASE_SHA"]
schema_release = os.environ["SCHEMA_RELEASE"]
artifact_manifest_sha = os.environ["ARTIFACT_MANIFEST_SHA256"]
if re.fullmatch(r"[0-9a-f]{40}", release_sha) is None:
    raise SystemExit("invalid RELEASE_SHA")
if not schema_release.strip() or any(character in schema_release for character in "\r\n\t\0"):
    raise SystemExit("invalid SCHEMA_RELEASE")
if re.fullmatch(r"[0-9a-f]{64}", artifact_manifest_sha) is None:
    raise SystemExit("invalid ARTIFACT_MANIFEST_SHA256")
Path("/app/release-build-info.json").write_text(
    json.dumps(
        {
            "releaseSha": release_sha,
            "schemaRelease": schema_release,
            "artifactManifestSha256": artifact_manifest_sha,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n",
    encoding="utf-8",
)
PY

USER 10001:10001

CMD ["python", "main.py"]
