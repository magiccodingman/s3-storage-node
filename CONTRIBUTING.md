# Contributing

S3 Storage Node is intentionally conservative around storage behavior. Changes that affect mounting, durability, service shutdown, target identity, or write acknowledgment should include tests and a clear description of the failure mode being addressed.

Run the local checks with:

```bash
make test
```

Use a feature branch and open a pull request against `main`. The `release` branch is reserved for publishing tested revisions to Docker Hub.
