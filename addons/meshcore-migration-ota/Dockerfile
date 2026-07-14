FROM ghcr.io/home-assistant/base:3.22

ARG BUILD_ARCH
ARG BUILD_VERSION

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apk add --no-cache python3 py3-pip \
    && apk add --no-cache --virtual .build-deps build-base \
    && python3 -m venv /opt/venv

COPY worker-launcher.c /tmp/worker-launcher.c
RUN cc -O2 -Wall -Wextra -Werror /tmp/worker-launcher.c -o /usr/local/bin/meshcore-ota-worker \
    && rm /tmp/worker-launcher.c \
    && apk del .build-deps

COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir --requirement /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY app /usr/share/meshcore-migration-ota/app
COPY rootfs /

RUN chmod a+x \
    /etc/services.d/meshcore-migration-ota/run \
    /etc/services.d/meshcore-ota-worker/run \
    /etc/services.d/meshcore-ota-worker/finish

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"
