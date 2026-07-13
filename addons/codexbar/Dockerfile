ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM ${BUILD_FROM}

ARG BUILD_ARCH
ARG BUILD_VERSION
ARG CODEXBAR_VERSION=0.41.0

ENV BUILD_ARCH=${BUILD_ARCH}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apk add --no-cache ca-certificates curl jq nodejs npm python3 tar \
    && npm install -g @openai/codex@0.143.0 @anthropic-ai/claude-code@2.1.205 \
    && npm cache clean --force \
    && mkdir -p /usr/share/codexbar-addon

COPY rootfs /

RUN chmod a+x /etc/services.d/codexbar/run /etc/services.d/codexbar/finish \
    && chmod a+x /etc/services.d/setup-ui/run /etc/services.d/setup-ui/finish \
    && chmod a+x /usr/local/bin/install-codexbar-cli \
    && /usr/local/bin/install-codexbar-cli "${CODEXBAR_VERSION}"

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8099/health >/dev/null || exit 1
