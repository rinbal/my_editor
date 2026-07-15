#!/usr/bin/env bash
#
# One-command release for MyEditor.
#
# Usage:
#   packaging/release.sh <version> [--yes]
#   packaging/release.sh 3.0
#   packaging/release.sh v3.0 --yes
#
# Preconditions (checked, the script stops if any fail):
#   1. The working tree is clean. Commit all code for this release first.
#   2. docs/releases/v<version>.md exists and is committed. Write it first.
#   3. The tag v<version> does not already exist locally or on origin.
#
# What it does:
#   1. Bumps APP_VERSION in constants.py to <version>.
#   2. Commits that bump as "Release v<version>".
#   3. Pushes the current commit to origin/main (fast-forward only).
#   4. Creates the tag v<version> and pushes it.
#
# Pushing the tag triggers .github/workflows/build-installers.yml, which builds
# the Windows, macOS (Apple Silicon + Intel) and Linux one-click installers on
# their own runners and publishes a GitHub Release that uses
# docs/releases/v<version>.md as the body and attaches the installers as assets.
# The three OS installers can only be built on their own platforms, so there is
# nothing to build locally. Everything here needs only git and python.

set -euo pipefail

# --- Parse arguments -----------------------------------------------------
YES=0
POSITIONAL=()
for arg in "$@"; do
    case "$arg" in
        -y|--yes) YES=1 ;;
        *) POSITIONAL+=("$arg") ;;
    esac
done

if [ "${#POSITIONAL[@]}" -ne 1 ]; then
    echo "usage: packaging/release.sh <version> [--yes]   (for example: packaging/release.sh 3.0)" >&2
    exit 2
fi

VERSION="${POSITIONAL[0]#v}"   # accept "3.0" or "v3.0"
TAG="v${VERSION}"

cd "$(git rev-parse --show-toplevel)"
NOTES="docs/releases/${TAG}.md"

# --- Preconditions -------------------------------------------------------
if [ -n "$(git status --porcelain)" ]; then
    echo "error: working tree is not clean. Commit all code and the release notes first." >&2
    exit 1
fi

if [ ! -f "$NOTES" ]; then
    echo "error: $NOTES not found. Write the release notes first." >&2
    exit 1
fi

if ! git ls-files --error-unmatch "$NOTES" >/dev/null 2>&1; then
    echo "error: $NOTES is not committed. Commit it first." >&2
    exit 1
fi

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
    echo "error: tag ${TAG} already exists locally." >&2
    exit 1
fi

if git ls-remote --exit-code --tags origin "${TAG}" >/dev/null 2>&1; then
    echo "error: tag ${TAG} already exists on origin." >&2
    exit 1
fi

# --- Confirm (this pushes to main and publishes a public release) --------
echo "Release ${TAG}"
echo "  notes:  ${NOTES}"
echo "  action: bump APP_VERSION to ${VERSION}, push HEAD to origin/main, push tag ${TAG}"
echo "  then:   CI builds the installers and publishes the release"
echo
if [ "$YES" -ne 1 ]; then
    if [ -t 0 ]; then
        read -r -p "Proceed? [y/N] " answer
        case "$answer" in
            y|Y|yes|YES) ;;
            *) echo "aborted."; exit 1 ;;
        esac
    else
        echo "error: non-interactive run without --yes. Re-run with --yes to confirm." >&2
        exit 1
    fi
fi

# --- Version bump --------------------------------------------------------
python3 - "$VERSION" <<'PY'
import re, sys
version = sys.argv[1]
path = "constants.py"
with open(path, encoding="utf-8") as f:
    src = f.read()
new, n = re.subn(r'^APP_VERSION = ".*"', f'APP_VERSION = "{version}"', src, count=1, flags=re.M)
if n != 1:
    sys.exit("error: could not find the APP_VERSION assignment in constants.py")
with open(path, "w", encoding="utf-8") as f:
    f.write(new)
PY
echo "Bumped APP_VERSION to ${VERSION}."

git add constants.py
git commit -m "Release ${TAG}"

# --- Push to main and tag ------------------------------------------------
echo "Pushing to origin/main ..."
git push origin HEAD:main

echo "Tagging ${TAG} ..."
git tag "${TAG}"
git push origin "${TAG}"

cat <<EOF

Done. Tag ${TAG} is pushed and CI is now building the installers.
Watch it here:
  https://github.com/rinbal/my_editor/actions
The release will appear here when the build finishes:
  https://github.com/rinbal/my_editor/releases/tag/${TAG}
EOF
