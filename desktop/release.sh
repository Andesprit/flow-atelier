#!/bin/bash
set -euo pipefail

signing_mode() {
  local key count=0
  for key in MACOS_CERTIFICATE MACOS_CERTIFICATE_PASSWORD MACOS_SIGNING_IDENTITY \
             MACOS_NOTARY_KEY MACOS_NOTARY_KEY_ID MACOS_NOTARY_ISSUER; do
    if [[ -n "${!key:-}" ]]; then count=$((count + 1)); fi
  done
  case "$count" in
    0) echo unsigned ;;
    6) echo signed ;;
    *) echo "Incomplete macOS signing configuration: set all six secrets or leave all six empty." >&2
       return 1 ;;
  esac
}

# The test sources only the credential selection above.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then return; fi
cd "$(dirname "$0")/.."
mode=$(signing_mode)
scratch=$(mktemp -d)
keychain="$scratch/desktop.keychain-db"
cleanup() {
  if [[ -f "$keychain" ]]; then
    security list-keychains -d user -s "${original_keychains[@]}"
    security delete-keychain "$keychain"
  fi
  rm -rf "$scratch"
}
trap cleanup EXIT

if [[ "$mode" = signed ]]; then
  original_keychains=()
  while IFS= read -r path; do original_keychains+=("$path"); done < <(
    security list-keychains -d user | sed -E 's/^[[:space:]]*"//; s/"$//'
  )
  password=$(openssl rand -hex 32)
  printf '%s' "$MACOS_CERTIFICATE" | base64 -D > "$scratch/cert.p12"
  printf '%s' "$MACOS_NOTARY_KEY" | base64 -D > "$scratch/notary.p8"
  security create-keychain -p "$password" "$keychain"
  security set-keychain-settings -lut 21600 "$keychain"
  security unlock-keychain -p "$password" "$keychain"
  security import "$scratch/cert.p12" -k "$keychain" -P "$MACOS_CERTIFICATE_PASSWORD" -T /usr/bin/codesign
  security set-key-partition-list -S apple-tool:,apple: -s -k "$password" "$keychain" >/dev/null
  security list-keychains -d user -s "$keychain" "${original_keychains[@]}"
  unset MACOS_CERTIFICATE MACOS_CERTIFICATE_PASSWORD MACOS_NOTARY_KEY password
  desktop/build.sh "$MACOS_SIGNING_IDENTITY"
else
  echo "Building unsigned developer DMG (no macOS signing secrets configured)."
  unset MACOS_CERTIFICATE MACOS_CERTIFICATE_PASSWORD MACOS_NOTARY_KEY
  desktop/build.sh
fi

app="desktop/target/release/bundle/macos/Flow Atelier.app"
dmg="desktop/build/Flow-Atelier-macos-arm64-$mode.dmg"
codesign --verify --deep --strict "$app"
mkdir "$scratch/home"
HOME="$scratch/home" ATELIER_NO_UPDATE_CHECK=1 "$app/Contents/Resources/atelier/atelier" --version
ATELIER_TEST_DESKTOP_SIDECAR="$PWD/$app/Contents/Resources/atelier/atelier" \
  uv run --locked pytest -q tests/cli/test_serve_idle_exit.py::test_desktop_parent_death_stops_server_and_active_shell
notarize() {
  xcrun notarytool submit "$1" --key "$scratch/notary.p8" --key-id "$MACOS_NOTARY_KEY_ID" \
    --issuer "$MACOS_NOTARY_ISSUER" --wait --timeout 30m --output-format json > "$scratch/result.json"
  cat "$scratch/result.json"
  test "$(plutil -extract status raw -o - "$scratch/result.json")" = Accepted
}
if [[ "$mode" = signed ]]; then
  ditto -c -k --keepParent "$app" "$scratch/app.zip"
  notarize "$scratch/app.zip"
  xcrun stapler staple "$app"
  xcrun stapler validate "$app"
  spctl --assess --type execute --verbose "$app"
fi
mkdir "$scratch/image"
ditto "$app" "$scratch/image/Flow Atelier.app"
hdiutil create -ov -format UDZO -fs HFS+ -volname "Flow Atelier" -srcfolder "$scratch/image" "$dmg"
if [[ "$mode" = signed ]]; then
  codesign --force --timestamp --sign "$MACOS_SIGNING_IDENTITY" "$dmg"
  notarize "$dmg"
  xcrun stapler staple "$dmg"
  xcrun stapler validate "$dmg"
  spctl --assess --type open --context context:primary-signature --verbose "$dmg"
fi
echo "$dmg"
