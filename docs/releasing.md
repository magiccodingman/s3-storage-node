# Release workflow

The repository uses two long-lived branches:

- `main` — normal development and pull requests
- `release` — every push publishes a Docker image

## Automatic versions

The release workflow calculates the next semantic version from Git tags:

- first release: `0.1.0`
- default change: patch bump
- commit containing `[minor]` or a conventional `feat:` subject: minor bump
- commit containing `[major]`, `[breaking]`, or `BREAKING CHANGE`: major bump

If a workflow is rerun for a commit that already has a version tag, it reuses that version rather than incrementing again.

## Published tags

For version `1.4.2`, the workflow publishes:

```text
latest
1.4.2
1.4
1
```

Images are built for:

```text
linux/amd64
linux/arm64
```

The workflow also creates an annotated Git tag and updates the Docker Hub repository description from the repository `README.md`.

## Required repository secrets

```text
DOCKERHUB_USERNAME
DOCKERHUB_TOKEN
```

## Publishing

Merge the desired tested commit from `main` into `release`. The push to `release` triggers `.github/workflows/release.yml`.
