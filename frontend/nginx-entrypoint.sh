#!/bin/sh
set -eu

api_upstream="${API_UPSTREAM:-api}"
case "$api_upstream" in
    "" | -* | *- | *[!a-z0-9-]*)
        printf '%s\n' 'invalid API_UPSTREAM' >&2
        exit 64
        ;;
esac

mkdir -p /tmp/nginx-conf.d
sed "s/__API_UPSTREAM__/${api_upstream}/g" \
    /etc/aiops-nginx/default.conf.template \
    > /tmp/nginx-conf.d/default.conf

exec "$@"
