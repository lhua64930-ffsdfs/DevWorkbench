FROM nginx:1.30.6-alpine-slim@sha256:f65ed258206d1eb6c4eb6141111715a2cf94ac0a4ec9675fc2da186f7aadb163

RUN apk upgrade --no-cache \
    && apk info -e 'zlib>=1.4-r0' \
    && apk info -e 'musl>=1.2.6-r3'
