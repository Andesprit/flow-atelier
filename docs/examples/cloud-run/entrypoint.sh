#!/usr/bin/env bash
# Clone a repository, prove both logins work, run one conduit, keep its flows.
#
# Environment:
#   REPO                     owner/name of the repository to work in (required)
#   CONDUIT                  conduit to run (required)
#   ATELIER_ARGS             extra arguments for `atelier run`, e.g. "--input hours=6"
#   BRANCH                   branch to check out (default: the repository default)
#   GITHUB_TOKEN             token that can clone, and push if the conduit pushes
#   CLAUDE_CODE_OAUTH_TOKEN  from `claude setup-token`
#   CODEX_AUTH_FILE          mounted copy of a logged-in ~/.codex/auth.json
#   STATE_DIR                mounted bucket that keeps .atelier/flows (optional)
set -euo pipefail

: "${REPO:?set REPO to owner/name}"
: "${CONDUIT:?set CONDUIT to the conduit to run}"

# Codex rewrites auth.json when it refreshes its token, and a mounted
# secret is read-only, so it works from a copy.
if [ -n "${CODEX_AUTH_FILE:-}" ]; then
    mkdir -p "$CODEX_HOME"
    cp "$CODEX_AUTH_FILE" "$CODEX_HOME/auth.json"
    chmod 600 "$CODEX_HOME/auth.json"
fi

git config --global user.name "${GIT_AUTHOR_NAME:-flow-atelier}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-flow-atelier@users.noreply.github.com}"
git clone ${BRANCH:+--branch "$BRANCH"} \
    "https://x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git" work
cd work

# A login that opens a session can still fail its first real request; a
# one-line prompt per agent fails the job here instead of hours in.
atelier harness check claude-code --prompt
atelier harness check codex --prompt

status=0
# shellcheck disable=SC2086 # ATELIER_ARGS is a list of words on purpose
atelier run "$CONDUIT" ${ATELIER_ARGS:-} || status=$?

if [ -n "${STATE_DIR:-}" ] && [ -d .atelier/flows ]; then
    mkdir -p "$STATE_DIR/flows"
    cp -r .atelier/flows/. "$STATE_DIR/flows/"
fi
exit "$status"
