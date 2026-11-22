# Install

## One-command install (no Python needed)

The quickest way. The script downloads a prebuilt `atelier` binary into
`~/.atelier/bin`, verifies its SHA-256 checksum against the published
release, and adds it to your `PATH`. It is safe to re-run to upgrade.

Once a day the binary checks GitHub for a newer release and, if one
exists, says so on stderr. Nothing is installed until you run
`atelier self-update`. Set `ATELIER_NO_UPDATE_CHECK=1` to silence the
check.

**macOS (Apple Silicon) / Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/Andesprit/flow-atelier/main/install.sh | bash
export PATH="$HOME/.atelier/bin:$PATH"
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/Andesprit/flow-atelier/main/install.ps1 | iex
```

Prebuilt binaries are published for **Linux x86_64**, **macOS arm64
(Apple Silicon)**, and **Windows x86_64**. Intel Macs are not supported
(no Rosetta fallback exists for an arm64 binary). Open a new terminal
after installing on Windows so the updated `PATH` takes effect.

## Install with uv (for Python users)

If you have Python 3.13+ and [uv](https://docs.astral.sh/uv/), you can
install from PyPI instead:

```bash
uv tool install flow-atelier
uv tool upgrade flow-atelier      # upgrade later
uv tool uninstall flow-atelier    # remove
```

Either way, you end up with an `atelier` command on your `PATH`.

For agent tasks, [install and sign in to your AI harnesses](../configuration/agent-logins.md#optional-ai-harnesses).
