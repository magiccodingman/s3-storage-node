#!/usr/bin/env bash
set -euo pipefail
python3 -m compileall -q src
python3 -m pytest
python3 -m s3_storage_node.main validate --config config/config.toml.example
