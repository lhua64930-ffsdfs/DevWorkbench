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

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS pgdg-key
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gpg
RUN curl --fail --silent --show-error https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /pgdg.asc \
    && gpg --batch --no-autostart --with-colons --show-keys /pgdg.asc > /tmp/pgdg-keyinfo \
    && test "$(awk -F: '$1=="pub" {primary=1} $1=="fpr" && primary {print $10; primary=0}' /tmp/pgdg-keyinfo)" = B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY --from=pgdg-key /pgdg.asc /usr/share/keyrings/pgdg.asc
# Debian DSA-6613-1 backports the zlib fix to trixie.
RUN printf '%s\n' 'deb [signed-by=/usr/share/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main' > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' zlib1g)" ge '1:1.3.dfsg+really1.3.1-1+deb13u1' \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements-backup.lock /tmp/requirements-backup.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes --index-url https://pypi.org/simple -r /tmp/requirements-backup.lock \
    && python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip \
    && groupadd -g 10001 codepilot && useradd -u 10001 -g 10001 -M codepilot \
    && mkdir /work /restore_uploads && chown 10001:10001 /work /restore_uploads
COPY scripts/backup_restore.py /opt/codepilot/backup_restore.py
ENV TMPDIR=/work
RUN --mount=type=bind,from=krb5-build,source=/out,target=/packages \
    dpkg -i /packages/*.deb \
    && test -z "$(dpkg --audit)"
USER 10001:10001
ENTRYPOINT ["python", "/opt/codepilot/backup_restore.py"]
CMD ["--help"]
