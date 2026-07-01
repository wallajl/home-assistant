ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG BUILD_ARCH
ARG TTYD_VERSION=1.7.7

USER root

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        jq \
    && case "${BUILD_ARCH}" in \
        amd64) TTYD_ARCH="x86_64" ;; \
        aarch64) TTYD_ARCH="aarch64" ;; \
        *) echo "Unsupported build arch: ${BUILD_ARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${TTYD_ARCH}" -o /usr/local/bin/ttyd \
    && chmod +x /usr/local/bin/ttyd \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY rootfs /

RUN chmod +x /usr/local/bin/hermes-agent-addon /usr/local/bin/hermes-shell

ENTRYPOINT ["/usr/local/bin/hermes-agent-addon"]
