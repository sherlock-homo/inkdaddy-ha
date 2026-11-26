FROM node:22.23.5-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS node-base

FROM node-base AS frontend
WORKDIR /build
COPY hub/frontend/package.json hub/frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY hub/frontend/ ./
RUN npm run build

FROM node-base AS controller
WORKDIR /matter_controller
COPY matter_controller/package.json matter_controller/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund
COPY matter_controller/src/ src/

FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
WORKDIR /opt/inkdaddy
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INKDADDY_HA_MODE=supervisor \
    INKDADDY_DATA_DIR=/data/inkdaddy \
    INKDADDY_FRONTEND_DIST=/opt/inkdaddy/frontend \
    INKDADDY_DEFAULT_REFRESH_MINUTES=60 \
    INKDADDY_UDP_HEARTBEAT_ENABLED=0 \
    INKDADDY_UDP_FRAME_TRANSFER_ENABLED=0 \
    INKDADDY_VERSION=0.2.0-dev.29
# These are fixed, signed Debian snapshots. Allow their historical metadata to age.
RUN sed -i \
        -e 's|^URIs: http://deb.debian.org/debian$|URIs: https://snapshot.debian.org/archive/debian/20261120T000000Z/|' \
        -e 's|^URIs: http://deb.debian.org/debian-security$|URIs: https://snapshot.debian.org/archive/debian-security/20261120T000000Z/|' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get -o Acquire::Check-Valid-Until=false install -y --no-install-recommends fonts-dejavu-core libstdc++6 tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock.txt ./
RUN python -m pip install --no-cache-dir --no-compile --require-hashes --only-binary=:all: -r requirements.lock.txt
COPY README.md pyproject.toml ./
COPY hub/inkdaddy_hub/ hub/inkdaddy_hub/
COPY --from=frontend /build/dist/ frontend/
COPY --from=controller /usr/local/bin/node /usr/local/bin/node
COPY --from=controller /matter_controller/ matter_controller/
RUN python -m pip install --no-cache-dir --no-compile --no-deps --no-build-isolation . \
    && python -m pip check
CMD ["python", "-m", "inkdaddy_hub.ha_app"]
