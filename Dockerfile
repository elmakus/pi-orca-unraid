# syntax=docker/dockerfile:1
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS os

ARG UTC_DATE
# The resolved UTC day invalidates both update and upgrade, even with a warm cache.
RUN test -n "$UTC_DATE" \
    && apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
       bash ca-certificates curl wget git openssh-client ripgrep fd-find jq \
       python3 python3-venv dumb-init procps iproute2 iputils-ping dnsutils \
       netcat-openbsd less xdg-utils libnotify4 libnss3 libxss1 libxtst6 \
       libuuid1 libsecret-1-0 libgbm1 python3-gi gir1.2-atspi-2.0 \
       at-spi2-core xdotool xclip xvfb libstdc++6 libgcc-s1 \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && rm -rf /var/lib/apt/lists/*

FROM os AS runtime
ARG PI_VERSION
ARG PI_URL
ARG PI_SHA256
ARG ORCA_VERSION
ARG ORCA_URL
ARG ORCA_SHA256
ARG GH_VERSION
ARG GH_URL
ARG GH_SHA256

# Satisfy the official package's actual dependencies before extracting its full payload.
RUN test -n "$ORCA_VERSION" \
    && curl -fsSL --retry 3 --connect-timeout 20 "$ORCA_URL" -o /tmp/orca.deb \
    && printf '%s  /tmp/orca.deb\n' "$ORCA_SHA256" | sha256sum -c - \
    && apt-get update \
    && apt-get satisfy -y --no-install-recommends "$(dpkg-deb -f /tmp/orca.deb Depends)" \
    && dpkg-deb -x /tmp/orca.deb / \
    && ln -s /opt/Orca/resources/bin/orca-ide /usr/local/bin/orca-ide \
    && rm -rf /tmp/orca.deb /var/lib/apt/lists/*

RUN test -n "$PI_VERSION" && test -n "$GH_VERSION" \
    && curl -fsSL --retry 3 --connect-timeout 20 "$PI_URL" -o /tmp/pi.tar.gz \
    && printf '%s  /tmp/pi.tar.gz\n' "$PI_SHA256" | sha256sum -c - \
    && mkdir -p /opt/pi \
    && tar -xzf /tmp/pi.tar.gz -C /opt/pi --strip-components=1 \
    && ln -s /opt/pi/pi /usr/local/bin/pi \
    && curl -fsSL --retry 3 --connect-timeout 20 "$GH_URL" -o /tmp/gh.tar.gz \
    && printf '%s  /tmp/gh.tar.gz\n' "$GH_SHA256" | sha256sum -c - \
    && mkdir -p /opt/gh \
    && tar -xzf /tmp/gh.tar.gz -C /opt/gh --strip-components=1 \
    && ln -s /opt/gh/bin/gh /usr/local/bin/gh \
    && rm /tmp/pi.tar.gz /tmp/gh.tar.gz

RUN usermod -l agent -d /home/agent -m node \
    && groupmod -n agent node \
    && mkdir -p /workspace \
    && chown agent:agent /workspace /home/agent

ARG REVISION
ARG CREATED
ARG FINGERPRINT
ARG RUN_NUMBER
LABEL org.opencontainers.image.source="https://github.com/elmakus/pi-orca-unraid" \
      org.opencontainers.image.revision="$REVISION" \
      org.opencontainers.image.created="$CREATED" \
      org.opencontainers.image.version="pi-${PI_VERSION}-orca-${ORCA_VERSION}" \
      io.github.pi-orca.fingerprint="$FINGERPRINT" \
      io.github.pi-orca.run-number="$RUN_NUMBER"
COPY --chmod=644 build/resolved.json /usr/local/share/pi-orca/build.json
COPY --chmod=755 entrypoint.sh /usr/local/bin/start-pi-orca
ENV HOME=/home/agent \
    PI_PACKAGE_DIR=/opt/pi \
    NPM_CONFIG_PREFIX=/home/agent/.local/npm \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/home/agent/.local/bin:/home/agent/.local/npm/bin \
    ORCA_PORT=6769 \
    ORCA_PAIRING_ADDRESS=192.168.2.104
USER agent
WORKDIR /workspace
EXPOSE 6769
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=12s --start-period=120s --retries=3 \
    CMD timeout --kill-after=2s 8s orca-ide status >/dev/null 2>&1 || exit 1
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/usr/local/bin/start-pi-orca"]
CMD []
