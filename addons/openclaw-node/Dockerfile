ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG BUILD_ARCH
ARG OPENCLAW_VERSION=2026.6.11
ARG NODE_VERSION=22.19.0

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        jq \
        xz-utils \
    && case "${BUILD_ARCH}" in \
        amd64) NODE_ARCH="x64" ;; \
        aarch64) NODE_ARCH="arm64" ;; \
        *) echo "Unsupported build arch: ${BUILD_ARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.xz \
    && npm install -g "openclaw@${OPENCLAW_VERSION}" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY rootfs /

RUN chmod +x /usr/local/bin/openclaw-node

CMD ["/usr/local/bin/openclaw-node"]
