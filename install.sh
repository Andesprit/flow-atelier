#!/usr/bin/env bash
# install.sh — one-command installer for flow-atelier (Unix)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Andesprit/flow-atelier/main/install.sh | bash
#
# Downloads the latest binary for the current platform to ~/.atelier/bin/
# and adds it to PATH (idempotent).
set -euo pipefail

REPO="Andesprit/flow-atelier"
INSTALL_DIR="$HOME/.atelier/bin"

# --- Detect platform ---
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

case "${OS}-${ARCH}" in
    linux-x86_64)   ASSET="atelier-linux-x86_64" ;;
    darwin-arm64)   ASSET="atelier-macos-arm64" ;;
    darwin-x86_64)
        echo "Intel macOS (x86_64) is not supported: only an arm64 build is" >&2
        echo "published, and Rosetta 2 cannot run arm64 binaries on Intel." >&2
        exit 1 ;;
    *) echo "Unsupported platform: ${OS}-${ARCH}" >&2; exit 1 ;;
esac

# --- Fetch latest release tag ---
echo "Fetching latest release info..."
RELEASE_JSON="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest")"
TAG="$(echo "$RELEASE_JSON" | grep '"tag_name"' | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"

if [ -z "$TAG" ]; then
    echo "ERROR: could not determine latest release tag." >&2
    exit 1
fi

echo "Latest release: ${TAG}"

# --- Extract download URLs ---
ASSET_URL="$(echo "$RELEASE_JSON" | grep "\"browser_download_url\"" | grep "${ASSET}" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
SUMS_URL="$(echo "$RELEASE_JSON" | grep "\"browser_download_url\"" | grep "SHA256SUMS" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"

if [ -z "$ASSET_URL" ] || [ -z "$SUMS_URL" ]; then
    echo "ERROR: could not find download URLs for ${ASSET}." >&2
    exit 1
fi

# --- Download binary + checksums ---
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

echo "Downloading ${ASSET}..."
curl -fsSL -o "${TMPDIR}/${ASSET}" "$ASSET_URL"

echo "Downloading SHA256SUMS..."
curl -fsSL -o "${TMPDIR}/SHA256SUMS" "$SUMS_URL"

# --- Verify SHA-256 ---
EXPECTED="$(grep "  ${ASSET}$" "${TMPDIR}/SHA256SUMS" | awk '{print $1}')"
if [ -z "$EXPECTED" ]; then
    echo "ERROR: ${ASSET} not found in SHA256SUMS." >&2
    exit 1
fi

# Stock macOS ships `shasum`, not GNU coreutils' `sha256sum`. Pick whichever
# exists rather than skipping verification on the platform that lacks one.
if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL="$(sha256sum "${TMPDIR}/${ASSET}" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
    ACTUAL="$(shasum -a 256 "${TMPDIR}/${ASSET}" | awk '{print $1}')"
elif command -v openssl >/dev/null 2>&1; then
    ACTUAL="$(openssl dgst -sha256 "${TMPDIR}/${ASSET}" | awk '{print $NF}')"
else
    echo "ERROR: no SHA-256 tool found (need sha256sum, shasum, or openssl)." >&2
    exit 1
fi
if [ "$ACTUAL" != "$EXPECTED" ]; then
    echo "ERROR: SHA-256 mismatch!" >&2
    echo "  expected: ${EXPECTED}" >&2
    echo "  actual:   ${ACTUAL}" >&2
    exit 1
fi

echo "SHA-256 verified."

# --- Install ---
mkdir -p "$INSTALL_DIR"
cp "${TMPDIR}/${ASSET}" "${INSTALL_DIR}/atelier"
chmod +x "${INSTALL_DIR}/atelier"

# --- Add to PATH (idempotent, persistent) ---
# Key off the user's *login* shell ($SHELL), not the shell interpreting this
# script. Under `curl … | bash` the interpreter is always bash, so testing
# $BASH_VERSION would write to ~/.bashrc for every macOS user — a file zsh
# (the macOS default since Catalina) never reads, leaving `atelier` off PATH.
LOGIN_SHELL="$(basename "${SHELL:-}")"
case "$LOGIN_SHELL" in
    zsh)  SHELL_RC="${ZDOTDIR:-$HOME}/.zshrc" ;;
    bash) SHELL_RC="$HOME/.bashrc" ;;
    fish) SHELL_RC="$HOME/.config/fish/config.fish" ;;
    *)    SHELL_RC="$HOME/.profile" ;;
esac

if ! grep -qF "$INSTALL_DIR" "$SHELL_RC" 2>/dev/null; then
    mkdir -p "$(dirname "$SHELL_RC")"
    {
        echo ""
        echo "# Added by flow-atelier installer"
        if [ "$LOGIN_SHELL" = "fish" ]; then
            echo "set -gx PATH \$PATH $INSTALL_DIR"
        else
            echo "export PATH=\"\$PATH:$INSTALL_DIR\""
        fi
    } >> "$SHELL_RC"
    echo "Added ${INSTALL_DIR} to PATH in ${SHELL_RC}"
fi

# --- Also update the current session so 'atelier' works immediately ---
case ":${PATH}:" in
    *":${INSTALL_DIR}:"*) ;;
    *) export PATH="${PATH}:${INSTALL_DIR}" ;;
esac

# --- Warn if another 'atelier' is earlier on PATH ---
FOUND="$(command -v atelier 2>/dev/null || true)"
if [ -n "$FOUND" ] && [ "$FOUND" != "${INSTALL_DIR}/atelier" ]; then
    echo ""
    echo "WARNING: Another 'atelier' was found earlier on PATH:"
    echo "  ${FOUND}"
    echo "It will take priority over the installed binary."
fi

echo ""
echo "Installed atelier ${TAG} to ${INSTALL_DIR}/atelier"
