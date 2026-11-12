FROM milvusdb/milvus:v3.0.5@sha256:b9b79b4a284b41cd12d60abc418310f84621fa4c977d409b38f501519a7ccf8a AS milvus-build
USER root
ARG TARGETARCH
RUN --mount=type=cache,id=milvus-apt-lists,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=milvus-apt-cache,target=/var/cache/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && case "$TARGETARCH" in \
        amd64) ubuntu_archive=https://security.ubuntu.com/ubuntu ;; \
        arm64) ubuntu_archive=https://ports.ubuntu.com/ubuntu-ports ;; \
        *) exit 1 ;; \
    esac \
    && printf '%s\n' "deb $ubuntu_archive jammy main" \
        "deb $ubuntu_archive jammy-updates main" \
        "deb $ubuntu_archive jammy-security main" > /etc/apt/sources.list \
    && apt-get -o APT::Update::Error-Mode=any -o Acquire::Retries=2 update \
    && apt-get -o Acquire::Retries=2 install -y --no-install-recommends build-essential libssl-dev pkg-config
COPY deploy/security/build-milvus.sh /tmp/build-milvus.sh
RUN --mount=type=cache,target=/root/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    sh /tmp/build-milvus.sh "$TARGETARCH"

FROM milvusdb/milvus:v3.0.5@sha256:b9b79b4a284b41cd12d60abc418310f84621fa4c977d409b38f501519a7ccf8a AS runtime-files

USER root
# Use the upstream image's Ubuntu HTTPS sources and disable retries while upgrading APT.
RUN apt-get -o Acquire::Retries=0 update \
    && apt-get -o Acquire::Retries=0 install -y --no-install-recommends --only-upgrade apt libapt-pkg6.0 \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' apt)" ge '2.4.15ubuntu0.1' \
    && rm -rf /var/lib/apt/lists/*
COPY --from=milvus-build --chown=milvus:milvus --chmod=755 /out/milvus /milvus/bin/milvus
COPY --from=milvus-build --chown=milvus:milvus --chmod=755 /out/libmilvus-planparser.so /milvus/lib/libmilvus-planparser.so

# Export the merged runtime filesystem so the replaced binary is absent from the image layers.
FROM scratch
COPY --from=runtime-files / /
ENV PATH=/milvus/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LD_LIBRARY_PATH=/milvus/lib::/usr/lib \
    LD_PRELOAD=:/milvus/lib/libjemalloc.so \
    MALLOC_CONF=background_thread:true \
    OPENSSL_MODULES=/milvus/lib/ossl-modules \
    ASAN_OPTIONS=detect_leaks=1:detect_container_overflow=0
WORKDIR /milvus/
USER milvus:milvus
ENTRYPOINT ["/tini", "--"]
