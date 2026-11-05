#!/usr/bin/env bash
# Publish a Glacier release on GitHub.
#
#   tools/release.sh 0.3.0 [notes.md]
#
# Checks that master is clean and pushed and that glacier/__init__.py and pyproject.toml both say
# that version, tags v<version>, pushes the tag, builds glacier-<version>.tar.gz (git archive of
# the tag) plus SHA256SUMS, and creates the GitHub release with both files. `glacier self-update`
# and the GUI's update banner install exactly these two files.
set -euo pipefail

VERSION="${1:?usage: tools/release.sh VERSION [NOTES.md]}"
NOTES="${2:-}"
TAG="v$VERSION"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

die() { echo "release: $*" >&2; exit 1; }

[[ "$VERSION" =~ ^[0-9]+(\.[0-9]+)+$ ]] || die "version must look like 1.2.3"
[[ "$(git branch --show-current)" == master ]] || die "not on master"
[[ -z "$(git status --porcelain)" ]] || die "working tree is not clean"
git fetch -q origin
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/master)" ]] || die "master is not pushed (or is behind origin)"
grep -q "^__version__ = \"$VERSION\"$" glacier/__init__.py || die "glacier/__init__.py does not say $VERSION"
grep -q "^version = \"$VERSION\"$" pyproject.toml || die "pyproject.toml does not say $VERSION"
if gh release view "$TAG" >/dev/null 2>&1; then die "release $TAG already exists"; fi

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  [[ "$(git rev-parse "$TAG^{commit}")" == "$(git rev-parse HEAD)" ]] || die "tag $TAG exists on another commit"
else
  git tag -a "$TAG" -m "Glacier $VERSION"
fi
git push -q origin "refs/tags/$TAG"

OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
git archive --format=tar.gz --prefix="glacier-$VERSION/" -o "$OUT/glacier-$VERSION.tar.gz" "$TAG"
(cd "$OUT" && sha256sum "glacier-$VERSION.tar.gz" > SHA256SUMS)

args=(--title "Glacier $VERSION" --verify-tag)
if [[ -n "$NOTES" ]]; then args+=(--notes-file "$NOTES"); else args+=(--generate-notes); fi
gh release create "$TAG" "$OUT/glacier-$VERSION.tar.gz" "$OUT/SHA256SUMS" "${args[@]}"
