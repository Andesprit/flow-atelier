#!/bin/bash
set -eu
source desktop/release.sh
test "$(signing_mode)" = unsigned
export MACOS_CERTIFICATE=fixture
if signing_mode; then exit 1; fi
export MACOS_CERTIFICATE_PASSWORD=fixture MACOS_SIGNING_IDENTITY=fixture
export MACOS_NOTARY_KEY=fixture MACOS_NOTARY_KEY_ID=fixture MACOS_NOTARY_ISSUER=fixture
test "$(signing_mode)" = signed
