# install.ps1 — one-command installer for flow-atelier (Windows)
#
# Usage:
#   irm https://raw.githubusercontent.com/Andesprit/flow-atelier/main/install.ps1 | iex
#
# Downloads the latest binary for the current platform to
# %USERPROFILE%\.atelier\bin\ and adds it to the user-level PATH (idempotent).

$ErrorActionPreference = "Stop"

$Repo = "Andesprit/flow-atelier"
$InstallDir = Join-Path (Join-Path $env:USERPROFILE ".atelier") "bin"
$BinaryName = "atelier.exe"

# --- Detect platform ---
$AssetName = "atelier-windows-x86_64.exe"

# --- Fetch latest release info ---
Write-Host "Fetching latest release info..."
$ReleaseUrl = "https://api.github.com/repos/$Repo/releases/latest"
$Release = Invoke-RestMethod -Uri $ReleaseUrl -Headers @{ "User-Agent" = "flow-atelier-installer" }
$Tag = $Release.tag_name

if (-not $Tag) {
    Write-Error "Could not determine latest release tag."
    exit 1
}

Write-Host "Latest release: $Tag"

# --- Extract download URLs ---
$AssetUrl = $null
$SumsUrl = $null

foreach ($asset in $Release.assets) {
    if ($asset.name -eq $AssetName) {
        $AssetUrl = $asset.browser_download_url
    }
    elseif ($asset.name -eq "SHA256SUMS") {
        $SumsUrl = $asset.browser_download_url
    }
}

if (-not $AssetUrl -or -not $SumsUrl) {
    Write-Error "Could not find download URLs for $AssetName."
    exit 1
}

# --- Download binary + checksums ---
$TmpDir = Join-Path $env:TEMP "atelier-install-$(Get-Random)"
New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null
try {
    Write-Host "Downloading $AssetName..."
    $BinaryPath = Join-Path $TmpDir $AssetName
    Invoke-WebRequest -Uri $AssetUrl -OutFile $BinaryPath

    Write-Host "Downloading SHA256SUMS..."
    $SumsPath = Join-Path $TmpDir "SHA256SUMS"
    Invoke-WebRequest -Uri $SumsUrl -OutFile $SumsPath

    # --- Verify SHA-256 ---
    $SumsContent = Get-Content $SumsPath -Raw
    $ExpectedHash = $null
    foreach ($line in $SumsContent -split "`n") {
        $parts = $line -split "\s+"
        if ($parts.Length -ge 2 -and $parts[1] -eq $AssetName) {
            $ExpectedHash = $parts[0].ToLower()
            break
        }
    }

    if (-not $ExpectedHash) {
        Write-Error "$AssetName not found in SHA256SUMS."
        exit 1
    }

    $ActualHash = (Get-FileHash -Path $BinaryPath -Algorithm SHA256).Hash.ToLower()
    if ($ActualHash -ne $ExpectedHash) {
        Write-Error "SHA-256 mismatch!`n  expected: $ExpectedHash`n  actual:   $ActualHash"
        exit 1
    }

    Write-Host "SHA-256 verified."

    # --- Install ---
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    $DestPath = Join-Path $InstallDir $BinaryName
    Copy-Item -Path $BinaryPath -Destination $DestPath -Force

    # --- Add to PATH (idempotent, user-level) ---
    # A user with no user-level Path entry gets $null back, which would throw
    # on .EndsWith below.
    $CurrentPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $CurrentPath) { $CurrentPath = "" }
    if ($CurrentPath -notlike "*$InstallDir*") {
        $NewPath = if ($CurrentPath.EndsWith(";")) {
            "$CurrentPath$InstallDir"
        } else {
            "$CurrentPath;$InstallDir"
        }
        [System.Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        Write-Host "Added $InstallDir to user PATH."
    } else {
        Write-Host "$InstallDir already in PATH."
    }

    # --- Also update the current session so 'atelier' works immediately ---
    if ($env:Path -notlike "*$InstallDir*") {
        $env:Path = "$env:Path;$InstallDir"
    }

    # --- Warn if another 'atelier' is earlier on PATH ---
    $found = Get-Command atelier -ErrorAction SilentlyContinue
    if ($found -and $found.Source -ne $DestPath) {
        Write-Host ""
        Write-Host "WARNING: Another 'atelier' was found earlier on PATH:" -ForegroundColor Yellow
        Write-Host "  $($found.Source)" -ForegroundColor Yellow
        Write-Host "It will take priority over the installed binary." -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "Installed atelier $Tag to $DestPath"
}
finally {
    Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue
}
