# Flow Atelier for macOS arm64

Install Xcode Command Line Tools, Rust, uv, Python 3.13 and the official Tauri CLI. From the repository root run:

```sh
cargo install tauri-cli --version '=2.12.1' --locked
desktop/build.sh
open "desktop/target/release/bundle/macos/Flow Atelier.app"
```

The app bundles the dashboard shipped in the Python package and its matching PyInstaller onedir engine. Each launch starts its own loopback server on a kernel-assigned port. Finder launches use the user's login-shell PATH. If startup takes over three seconds or the shell fails, the app explains the fallback and uses its inherited PATH.

Closing the window leaves the app running. Use **Show Flow Atelier** in the menu bar to reopen it, or **Quit Flow Atelier** to stop its server. The existing global scheduler lease selects one dispatcher, including when a CLI daemon starts first. Allow notifications in macOS to see newly finished runs from either owner. Clicking a notice opens its run page.

## Direct distribution

The release workflow builds an arm64 disk image on version tags. Local developers can reproduce the unsigned image with `desktop/release.sh`. macOS arm64 binaries carry ad hoc signatures in this build. Its filename ends in `-unsigned.dmg`.

For a notarized release, use an Apple Developer Program team and a **Developer ID Application** certificate with its private key. Apple lists membership at **USD 99 per membership year** or local equivalent. [Apple membership](https://developer.apple.com/programs/whats-included/) · [Developer ID](https://developer.apple.com/developer-id/)

Configure these secrets in the protected GitHub Actions environment `macos-release`. Restrict deployment to trusted release tags and reviewers.

| Secret | Value |
| --- | --- |
| `MACOS_CERTIFICATE` | Base64 of the PKCS#12 export containing the Developer ID Application certificate and private key |
| `MACOS_CERTIFICATE_PASSWORD` | Password protecting that export |
| `MACOS_SIGNING_IDENTITY` | Full Developer ID Application identity or its certificate SHA-1 fingerprint |
| `MACOS_NOTARY_KEY` | Base64 of the App Store Connect team API private key (.p8) used by notarytool |
| `MACOS_NOTARY_KEY_ID` | API key ID |
| `MACOS_NOTARY_ISSUER` | Team API issuer UUID |

With all six values, the job imports the certificate into a temporary keychain. PyInstaller signs the Python executable and embedded Mach-O libraries. The job signs the assembled app with hardened runtime and a secure timestamp, checks its strict deep signature, requires Apple to accept it, staples the app, then signs, notarizes and staples the DMG. Both artifacts receive stapler and Gatekeeper checks. Missing secrets select the unsigned build. A failed signing or notarization attempt fails the release job. [Tauri macOS signing](https://v2.tauri.app/distribute/sign/macos/) · [Apple notarization](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)

For release acceptance, download the signed DMG on a clean Mac, drag the app to Applications, launch from Finder and run a conduit with a login-shell-only agent. Close the window before a scheduled fire, click its notification and verify the run ID. Repeat with a CLI daemon already holding the lease and confirm that it survives desktop Quit.
