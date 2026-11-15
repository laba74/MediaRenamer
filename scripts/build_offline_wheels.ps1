<#
.SYNOPSIS
    Downloads the wheels MediaRenamer needs so it can run on a machine without internet access.

.DESCRIPTION
    Run this script on a connected Windows machine, from any folder. It:

    1. exports the locked dependencies of uv.lock to <OutputDir>\requirements.txt
       (exact versions and hashes);
    2. downloads the matching wheels into <OutputDir>;
    3. downloads the standalone uv executable (same version as the local uv, checked
       against its published SHA-256) into <OutputDir>\uv, so that the offline machine
       does not need uv installed.

    Then copy the whole repository folder, <OutputDir> included, to the offline machine
    and follow the "Offline installation" section of the README.

    The wheels are downloaded for the platform running this script: build them on
    Windows for a Windows target.

.PARAMETER OutputDir
    Folder that receives the wheels and requirements.txt, relative to the repository
    root unless absolute. Its previous *.whl files and requirements.txt are removed
    first. Default: wheels.

.PARAMETER IncludeDev
    Also download the development tools (pytest and its dependencies), so that the
    test suite can run offline.

.PARAMETER UvArchitecture
    CPU architecture of the offline machine, for the bundled uv executable:
    x86_64 (default), aarch64 or i686.

.PARAMETER SkipUv
    Do not download the uv executable (the offline machine already has uv).

.EXAMPLE
    .\scripts\build_offline_wheels.ps1

.EXAMPLE
    .\scripts\build_offline_wheels.ps1 -IncludeDev -OutputDir D:\transfer\wheels
#>
[CmdletBinding()]
param(
    [string]$OutputDir = "wheels",
    [switch]$IncludeDev,
    [ValidateSet("x86_64", "aarch64", "i686")]
    [string]$UvArchitecture = "x86_64",
    [switch]$SkipUv
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv was not found on the PATH. Install it first: https://docs.astral.sh/uv/getting-started/installation/"
}

if (-not [System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir = Join-Path $repoRoot $OutputDir
}
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$OutputDir = (Resolve-Path $OutputDir).Path

# Start from a clean wheelhouse so that no stale version is left behind.
Get-ChildItem -Path $OutputDir -Filter *.whl -File | Remove-Item -Force -Confirm:$false
$requirements = Join-Path $OutputDir "requirements.txt"
if (Test-Path $requirements) {
    Remove-Item $requirements -Force -Confirm:$false
}

# 1. Export the locked dependencies (exact versions and hashes).
$exportArgs = @(
    "export", "--locked", "--no-emit-project", "--format", "requirements-txt",
    "--output-file", $requirements, "--quiet"
)
if (-not $IncludeDev) {
    $exportArgs += "--no-dev"
}
Write-Host "Exporting the locked dependencies to $requirements"
Push-Location $repoRoot
try {
    & uv @exportArgs
    if ($LASTEXITCODE -ne 0) {
        throw "uv export failed (exit code $LASTEXITCODE). Run 'uv lock' if uv.lock is out of date."
    }
}
finally {
    Pop-Location
}

# 2. Download the wheels. uv has no download command, so pip is run through uv.
#    The requirements are fully pinned, hence --no-deps; --only-binary refuses sdists,
#    which could not be built on a machine without build tools.
Write-Host "Downloading the wheels to $OutputDir"
& uv tool run pip download --requirement $requirements --dest $OutputDir --no-deps --only-binary=:all: --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    throw "Wheel download failed (exit code $LASTEXITCODE)."
}

# 3. Download the standalone uv executable, same version as the local one.
$uvDir = Join-Path $OutputDir "uv"
if (-not $SkipUv) {
    $uvVersionOutput = (& uv --version) -join " "
    if ($uvVersionOutput -notmatch '^uv (\d+\.\d+\.\d+)') {
        throw "Cannot read the uv version from '$uvVersionOutput'."
    }
    $uvVersion = $Matches[1]
    $archiveName = "uv-$UvArchitecture-pc-windows-msvc.zip"
    $releaseUrl = "https://github.com/astral-sh/uv/releases/download/$uvVersion"
    $downloadDir = Join-Path ([System.IO.Path]::GetTempPath()) ("uv-offline-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $downloadDir | Out-Null
    try {
        $archive = Join-Path $downloadDir $archiveName
        Write-Host "Downloading uv $uvVersion ($archiveName)"
        $ProgressPreference = "SilentlyContinue"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "$releaseUrl/$archiveName" -OutFile $archive -UseBasicParsing
        $checksumText = (Invoke-WebRequest -Uri "$releaseUrl/$archiveName.sha256" -UseBasicParsing).Content
        if ($checksumText -is [byte[]]) {
            $checksumText = [System.Text.Encoding]::ASCII.GetString($checksumText)
        }
        $expectedHash = ($checksumText.Trim() -split '\s+')[0]
        $actualHash = (Get-FileHash -Path $archive -Algorithm SHA256).Hash
        if ($actualHash -ne $expectedHash) {
            throw "SHA-256 mismatch for $archiveName (expected $expectedHash, got $actualHash)."
        }
        if (Test-Path $uvDir) {
            Remove-Item $uvDir -Recurse -Force -Confirm:$false
        }
        Expand-Archive -Path $archive -DestinationPath $uvDir
        if (-not (Test-Path (Join-Path $uvDir "uv.exe"))) {
            throw "uv.exe was not found in $archiveName."
        }
    }
    finally {
        Remove-Item $downloadDir -Recurse -Force -Confirm:$false -ErrorAction SilentlyContinue
    }
}

$wheels = @(Get-ChildItem -Path $OutputDir -Filter *.whl -File)
Write-Host ""
Write-Host "$($wheels.Count) wheel(s) in ${OutputDir}:"
$wheels | ForEach-Object { Write-Host "  $($_.Name)" }
Write-Host ""
# Show the wheel folder relative to the repository when it lives inside it, since the
# repository is copied elsewhere on the offline machine.
$findLinks = $OutputDir
$rootPrefix = $repoRoot.TrimEnd('\') + '\'
if ($OutputDir.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    $findLinks = $OutputDir.Substring($rootPrefix.Length)
}
Write-Host "Copy the repository folder (with the wheel folder) to the offline machine, then run there,"
Write-Host "from the repository root:"
if (-not $SkipUv) {
    $uvPathHint = if ([System.IO.Path]::IsPathRooted($findLinks)) { "$findLinks\uv" } else { "`$PWD\$findLinks\uv" }
    Write-Host "  `$env:PATH = `"$uvPathHint;`$env:PATH`""
}
# 'uv sync' is not used: it checks the whole lockfile, dev group included, even when
# only the runtime wheels are present. 'uv pip install' installs exactly the export.
Write-Host "  uv venv --offline"
Write-Host "  uv pip install --offline --no-index --find-links `"$findLinks`" --requirement `"$findLinks\requirements.txt`""
Write-Host "  uv run --offline --no-sync python media_renamer.py --check"
