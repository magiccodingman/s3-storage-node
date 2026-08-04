#!/bin/sh
set -eu

source_config="${S3_STORAGE_NODE_CONFIG:-/etc/s3-storage-node/config.toml}"
runtime_config="/run/s3-storage-node/transport-chaos.toml"
volume_max="${CHAOS_VOLUME_MAX:-32}"

mkdir -p "$(dirname "$runtime_config")"
sed "s/^volume_max = 2$/volume_max = ${volume_max}/" "$source_config" > "$runtime_config"
export S3_STORAGE_NODE_CONFIG="$runtime_config"

exec /usr/local/bin/s3-storage-node
