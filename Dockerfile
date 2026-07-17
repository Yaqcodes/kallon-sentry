# Kallon control plane (enrollment + Platform API) for Railway / containers.
#
# Build from repo root:
#   docker build -t kallon-enrollment-api -f Dockerfile .
#
# Runtime layout keeps Path(__file__).parents[3] == /app (repo root) so
# registry/ and scripts/kallon-gateway-add-peer.sh resolve as in production.

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        openssh-client \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY registry/requirements.txt /app/registry/requirements.txt
COPY infra/enrollment-api/requirements.txt /app/infra/enrollment-api/requirements.txt

RUN pip install --no-cache-dir \
      -r /app/infra/enrollment-api/requirements.txt \
      -r /app/registry/requirements.txt

COPY registry/ /app/registry/
COPY infra/enrollment-api/ /app/infra/enrollment-api/
COPY scripts/kallon-gateway-add-peer.sh /app/scripts/kallon-gateway-add-peer.sh
COPY infra/enrollment-api/deploy/railway/entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/scripts/kallon-gateway-add-peer.sh /app/entrypoint.sh \
    && mkdir -p /etc/kallon \
    && chmod 700 /etc/kallon

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KALLON_REGISTRY=postgres \
    KALLON_PEER_BACKEND=subprocess \
    KALLON_PROXY_VIA_HUB=1 \
    KALLON_OPS_SSH_IDENTITY_FILE=/etc/kallon/terra-hub-ops.pem

WORKDIR /app/infra/enrollment-api

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
