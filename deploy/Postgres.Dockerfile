FROM postgres:18-alpine@sha256:123b17998ccaf47e53b502e8c6a1e79afd8f579c6f3eec83c563da026f957395 AS libxml2-build
RUN --mount=type=cache,target=/var/cache/apk,sharing=locked \
    apk --timeout 180 --cache-packages add abuild build-base ca-certificates perl tar xz-dev zlib-dev
WORKDIR /src/libxml2
COPY deploy/security/libxml2.APKBUILD APKBUILD
RUN abuild-keygen -a -n \
    && cp /root/.abuild/*.rsa.pub /etc/apk/keys/ \
    && REPODEST=/packages JOBS=4 abuild -F \
    && mkdir /out \
    && cp /packages/src/*/libxml2-2*.apk /root/.abuild/*.rsa.pub /out/

FROM postgres:18-alpine@sha256:123b17998ccaf47e53b502e8c6a1e79afd8f579c6f3eec83c563da026f957395 AS runtime-files

# The upstream entrypoint uses gosu only when it starts as root.
RUN apk upgrade --no-cache \
    && apk info -e 'zlib>=1.4-r0' \
    && apk info -e 'musl>=1.2.6-r3' \
    && rm /usr/local/bin/gosu
RUN --mount=type=bind,from=libxml2-build,source=/out,target=/packages \
    keys="$(mktemp -d)" \
    && cp /etc/apk/keys/* /packages/*.rsa.pub "$keys/" \
    && apk add --no-network --keys-dir "$keys" /packages/libxml2-2*.apk \
    && rm -r "$keys"

# Copy the prepared filesystem so the shipped image has no layer containing gosu.
FROM scratch
COPY --from=runtime-files / /
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=en_US.utf8 \
    PG_MAJOR=18 \
    PG_VERSION=18.6 \
    PGDATA=/var/lib/postgresql/18/docker
USER 70:70
VOLUME /var/lib/postgresql
EXPOSE 5432
STOPSIGNAL SIGINT
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["postgres"]
