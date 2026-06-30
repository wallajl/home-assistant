ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        dbus-x11 \
        jq \
        novnc \
        openbox \
        procps \
        websockify \
        x11vnc \
        xvfb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY rootfs /

RUN chmod +x /usr/local/bin/telegram-chat-gui

CMD ["/usr/local/bin/telegram-chat-gui"]
