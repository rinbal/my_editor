#!/usr/bin/env bash
#
# Publish a beta of MyEditor from dev.
#
# Usage:
#   packaging/release-beta.sh <x.y.z-betaN> [commit] [--yes]
#   packaging/release-beta.sh 4.0.0-beta1
#   packaging/release-beta.sh v4.0.0-beta1 1a2b3c4 --yes
#
# Without a commit, the freshly fetched origin/dev is tagged.
#
# Preconditions (checked, the script stops if any fail):
#   1. The version has the form x.y.z-betaN. Stable versions go through
#      packaging/release.sh.
#   2. dev can be fetched from origin.
#   3. The commit is part of origin/dev.
#   4. APP_VERSION in constants.py at that commit is x.y.z.
#   5. The build workflow at that commit publishes a beta tag as a pre-release
#      that is not "Latest". GitHub builds a tag with the workflow of the tagged
#      commit, so an older one would offer the beta to everybody as an update.
#   6. The tag v<x.y.z-betaN> does not exist, neither locally nor on origin.
#
# What it does: shows the commit, asks, tags it as v<x.y.z-betaN> and pushes
# only that tag. No branch and no file in the working tree changes, so open
# changes there do not matter.
#
# Pushing the tag triggers .github/workflows/build-installers.yml, which builds
# the installers and publishes them as a GitHub pre-release.

set -euo pipefail

usage() {
    echo "usage: packaging/release-beta.sh <x.y.z-betaN> [commit] [--yes]   (for example: packaging/release-beta.sh 4.0.0-beta1)" >&2
    exit 2
}

fail() {
    echo "error: $*" >&2
    exit 1
}

# --- Parse arguments -----------------------------------------------------
YES=0
POSITIONAL=()
for arg in "$@"; do
    case "$arg" in
        -y|--yes) YES=1 ;;
        -*) usage ;;
        *) POSITIONAL+=("$arg") ;;
    esac
done

if [ "${#POSITIONAL[@]}" -lt 1 ] || [ "${#POSITIONAL[@]}" -gt 2 ]; then
    usage
fi

VERSION="${POSITIONAL[0]#v}"   # accept "4.0.0-beta1" or "v4.0.0-beta1"
TAG="v${VERSION}"

if ! [[ "$VERSION" =~ ^([0-9]+\.[0-9]+\.[0-9]+)-beta[1-9][0-9]*$ ]]; then
    fail "${VERSION} is not a beta version (x.y.z-betaN, for example 4.0.0-beta1). Stable versions are released with packaging/release.sh."
fi
BASE_VERSION="${BASH_REMATCH[1]}"

cd "$(git rev-parse --show-toplevel)"
WORKFLOW=".github/workflows/build-installers.yml"
DEV="refs/remotes/origin/dev"

# --- The commit ----------------------------------------------------------
echo "Fetching dev from origin ..."
git fetch --quiet --no-tags origin "+refs/heads/dev:${DEV}" \
    || fail "could not fetch dev from origin."

if [ "${#POSITIONAL[@]}" -eq 2 ]; then
    REF="${POSITIONAL[1]}"
else
    REF="$DEV"
fi

COMMIT="$(git rev-parse --verify --quiet "${REF}^{commit}")" \
    || fail "there is no commit named '${REF}'."

git merge-base --is-ancestor "$COMMIT" "$DEV" \
    || fail "commit ${COMMIT} is not on origin/dev. Merge it into dev first."

# --- Preconditions -------------------------------------------------------
CONSTANTS="$(git show "${COMMIT}:constants.py")" \
    || fail "could not read constants.py at commit ${COMMIT}."
APP_VERSION="$(sed -n 's/^APP_VERSION = "\([^"]*\)".*/\1/p' <<<"$CONSTANTS")"
if [ "$APP_VERSION" != "$BASE_VERSION" ]; then
    fail "APP_VERSION at commit ${COMMIT} is '${APP_VERSION}', but ${TAG} needs '${BASE_VERSION}'."
fi

WORKFLOW_TEXT="$(git show "${COMMIT}:${WORKFLOW}")" \
    || fail "could not read ${WORKFLOW} at commit ${COMMIT}."
for setting in prerelease make_latest; do
    grep -qE "^[[:space:]]+${setting}:.*'-beta'" <<<"$WORKFLOW_TEXT" \
        || fail "${WORKFLOW} at commit ${COMMIT} does not set ${setting} for beta tags. It would publish ${TAG} as a normal release."
done

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
    fail "tag ${TAG} already exists locally."
fi

# ls-remote exits with 2 when nothing matches. Any other failure means origin
# did not answer, which must not pass for "the tag is free".
status=0
git ls-remote --exit-code --tags origin "refs/tags/${TAG}" >/dev/null || status=$?
case "$status" in
    0) fail "tag ${TAG} already exists on origin." ;;
    2) ;;
    *) fail "could not ask origin whether tag ${TAG} exists." ;;
esac

# --- Confirm (this publishes a public pre-release) -----------------------
NEWER="$(git rev-list --count "${COMMIT}..${DEV}")"
echo
echo "Beta release ${TAG}"
echo "  commit: $(git log -1 --format='%h %s' "$COMMIT")"
echo "  date:   $(git log -1 --format='%cd' --date=iso "$COMMIT")"
if [ "$NEWER" -ne 0 ]; then
    echo "  note:   origin/dev has ${NEWER} newer commit(s) that are not part of this beta"
fi
echo "  action: tag this commit and push only the tag ${TAG} to origin"
echo "  then:   CI builds the installers and publishes a pre-release; \"Latest\" stays as it is"
echo
if [ "$YES" -ne 1 ]; then
    if [ -t 0 ]; then
        read -r -p "Proceed? [y/N] " answer
        case "$answer" in
            y|Y|yes|YES) ;;
            *) echo "aborted."; exit 1 ;;
        esac
    else
        fail "non-interactive run without --yes. Re-run with --yes to confirm."
    fi
fi

# --- Tag -----------------------------------------------------------------
echo "Tagging ${TAG} ..."
git tag "${TAG}" "${COMMIT}"
if ! git push --no-follow-tags origin "refs/tags/${TAG}"; then
    git tag -d "${TAG}" >/dev/null
    fail "could not push tag ${TAG}. The local tag was removed, so you can run this again."
fi

cat <<EOF

Done. Tag ${TAG} is pushed and CI is now building the installers.
Watch it here:
  https://github.com/rinbal/my_editor/actions
The pre-release will appear here when the build finishes:
  https://github.com/rinbal/my_editor/releases/tag/${TAG}
EOF
