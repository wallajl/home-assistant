ARG BUILD_FROM
FROM ${BUILD_FROM}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        chromium-sandbox \
        dbus-x11 \
        jq \
        novnc \
        openbox \
        procps \
        tigervnc-standalone-server \
        websockify \
        xdotool \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r snapchat \
    && useradd -r -g snapchat -d /data/home -s /bin/bash snapchat

COPY rootfs /

RUN chmod +x /usr/local/bin/snapchat-chat-gui

CMD ["/usr/local/bin/snapchat-chat-gui"]
