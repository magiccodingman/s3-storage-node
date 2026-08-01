.PHONY: test validate build

test:
	PYTHONPATH=src ./scripts/test.sh

validate:
	PYTHONPATH=src python3 -m s3_storage_node.main validate --config config/config.toml.example

build:
	docker build -t s3-storage-node:dev .
