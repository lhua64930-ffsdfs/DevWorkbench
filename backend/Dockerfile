FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS krb5-build
RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/var/cache/apt,sharing=locked \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get -o Acquire::https::Timeout=30 -o Acquire::Retries=2 update \
    && apt-get -o Acquire::https::Timeout=45 -o Acquire::Retries=2 install -y --no-install-recommends \
        build-essential bison ca-certificates comerr-dev curl debhelper docbook-to-man \
        keyutils libcmocka-dev libkeyutils-dev libldap2-dev liblmdb-dev libnss-myhostname \
        libsasl2-dev libssl-dev libverto-dev pkgconf python3-kdcproxy python3-pyrad ss-dev
COPY deploy/security/build-krb5.sh /tmp/build-krb5.sh
RUN sh /tmp/build-krb5.sh

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS expat-build
RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/var/cache/apt,sharing=locked \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get -o Acquire::https::Timeout=30 -o Acquire::Retries=2 update \
    && apt-get -o Acquire::https::Timeout=45 -o Acquire::Retries=2 install -y --no-install-recommends \
        build-essential ca-certificates curl debhelper docbook-to-man docbook2x
COPY deploy/security/build-expat.sh /tmp/build-expat.sh
RUN sh /tmp/build-expat.sh

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# Debian DSA-6613-1 backports the zlib fix to trixie.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' zlib1g)" ge '1:1.3.dfsg+really1.3.1-1+deb13u1' \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app/backend
COPY backend/requirements-production.lock ./
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes --index-url https://pypi.org/simple -r requirements-production.lock
COPY backend/security_patches ./security_patches
RUN python -m security_patches --apply && python -m security_patches
RUN python -m pip uninstall -y pip && rm -rf /usr/local/lib/python3.12/ensurepip
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
COPY docs/datas /app/docs/datas
RUN groupadd -g 10001 codepilot && useradd -u 10001 -g 10001 -M codepilot && mkdir -p /data/repos /data/uploads && chown -R 10001:10001 /data
RUN --mount=type=bind,from=expat-build,source=/out,target=/packages \
    dpkg -i /packages/libexpat1_*.deb \
    && install -m 755 /packages/pyexpat*.so /usr/local/lib/python3.12/lib-dynload/ \
    && python -c 'import pyexpat; assert pyexpat.EXPAT_VERSION == "expat_2.10.0"'
RUN --mount=type=bind,from=krb5-build,source=/out,target=/packages \
    dpkg -i /packages/*.deb \
    && test -z "$(dpkg --audit)"
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-server-header", "--timeout-graceful-shutdown", "30"]
