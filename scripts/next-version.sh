#!/usr/bin/env bash
set -euo pipefail

existing="$(git tag --points-at HEAD --list 'v[0-9]*' | sort -V | tail -n1)"
if [[ -n "$existing" ]]; then
  echo "${existing#v}"
  exit 0
fi

latest="$(git tag --list 'v[0-9]*' | sort -V | tail -n1)"
if [[ -z "$latest" ]]; then
  echo "0.1.0"
  exit 0
fi

version="${latest#v}"
IFS=. read -r major minor patch <<<"$version"
messages="$(git log "${latest}..HEAD" --pretty=%B)"
if grep -Eqi '\[(major|breaking)\]|BREAKING CHANGE' <<<"$messages"; then
  major=$((major + 1)); minor=0; patch=0
elif grep -Eqi '\[minor\]|^feat(\(.+\))?!?:' <<<"$messages"; then
  minor=$((minor + 1)); patch=0
else
  patch=$((patch + 1))
fi
printf '%s.%s.%s\n' "$major" "$minor" "$patch"
