# syntax=docker/dockerfile:1.7
ARG SEAWEEDFS_VERSION=4.40
FROM chrislusf/seaweedfs:${SEAWEEDFS_VERSION} AS seaweedfs

FROM debian:13.6-slim
ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="S3 Storage Node" \
      org.opencontainers.image.description="Fail-closed, self-healing SeaweedFS S3 appliance for CIFS, block devices, and managed storage paths" \
      org.opencontainers.image.source="https://github.com/magiccodingman/s3-storage-node" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       cifs-utils \
       e2fsprogs \
       haproxy \
       iproute2 \
       iptables \
       python3 \
       tini \
       util-linux \
       xfsprogs \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 seaweed \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/s3-storage-node --no-create-home --shell /usr/sbin/nologin seaweed \
    && mkdir -p /opt/s3-storage-node/src /etc/s3-storage-node /var/lib/s3-storage-node \
    && chown 10001:10001 /var/lib/s3-storage-node

COPY --from=seaweedfs /usr/bin/weed /usr/local/bin/weed
COPY src/ /opt/s3-storage-node/src/
COPY docker-entrypoint.sh /usr/local/bin/s3-storage-node

ENV PYTHONPATH=/opt/s3-storage-node/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    S3_STORAGE_NODE_CONFIG=/etc/s3-storage-node/config.toml \
    S3_STORAGE_NODE_VERSION=${APP_VERSION}

EXPOSE 8333 9090
VOLUME ["/var/lib/s3-storage-node"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=2 \
  CMD python3 -m s3_storage_node.main health --config "${S3_STORAGE_NODE_CONFIG}" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/s3-storage-node"]
