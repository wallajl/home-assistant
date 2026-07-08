ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM ${BUILD_FROM}

ARG BUILD_ARCH
ARG BUILD_VERSION
ARG CODEXBAR_VERSION=0.41.0

ENV BUILD_ARCH=${BUILD_ARCH}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apk add --no-cache ca-certificates curl jq nginx tar \
    && mkdir -p /run/nginx /usr/share/codexbar-addon /etc/nginx/http.d

COPY rootfs /

RUN chmod a+x /etc/services.d/codexbar/run /etc/services.d/codexbar/finish \
    && chmod a+x /etc/services.d/nginx/run /etc/services.d/nginx/finish \
    && chmod a+x /usr/local/bin/install-codexbar-cli \
    && /usr/local/bin/install-codexbar-cli "${CODEXBAR_VERSION}"

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"
