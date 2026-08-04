#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
  exec python3 -m s3_storage_node.main "$@"
fi

exec python3 -m s3_storage_node.main run --config "${S3_STORAGE_NODE_CONFIG:-/etc/s3-storage-node/config.toml}"
