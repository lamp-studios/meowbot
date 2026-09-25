# meowbot setup script
# creates the venv, installs deps with uv, then optionally runs the bot

$ErrorActionPreference = "Stop"

# always run from the project root (the folder above this script)
Set-Location (Split-Path $PSScriptRoot -Parent)

$EntryCandidates = @("main.py")

function Write-Step($msg) { Write-Host "`n>> $msg" -ForegroundColor Magenta }
function Write-Ok($msg)   { Write-Host "   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "   $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "   $msg" -ForegroundColor Red }

Write-Host "=== meowbot setup ===" -ForegroundColor Cyan

# 1. check uv
Write-Step "checking for uv"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Err "uv not found. install it with:"
    Write-Host '   powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    exit 1
}
Write-Ok "found $(uv --version)"

# 2. create venv
Write-Step "setting up venv"
if (Test-Path ".venv") {
    Write-Ok ".venv already exists, reusing it"
} else {
    uv venv
    if ($LASTEXITCODE -ne 0) { Write-Err "failed to create venv"; exit 1 }
    Write-Ok "created .venv"
}

# 3. install deps
Write-Step "installing dependencies"
if (Test-Path "uv.lock") {
    Write-Ok "uv.lock found, syncing from lockfile"
    uv sync --locked
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "lockfile is out of date with pyproject.toml, falling back to a normal sync"
        uv sync
    }
} elseif (Test-Path "pyproject.toml") {
    Write-Warn "no uv.lock, syncing from pyproject.toml (this will create one)"
    uv sync
} elseif (Test-Path "requirements.txt") {
    Write-Warn "no pyproject.toml, installing from requirements.txt"
    uv pip install -r requirements.txt
} else {
    Write-Err "no uv.lock, pyproject.toml or requirements.txt found. nothing to install"
    exit 1
}
if ($LASTEXITCODE -ne 0) { Write-Err "dependency install failed"; exit 1 }
Write-Ok "deps installed"

# 4. sanity checks
if (-not (Test-Path ".env")) {
    Write-Warn "no .env file found, the bot will probably scream about a missing token"
}

$Entry = $EntryCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Entry) {
    Write-Err "couldn't find an entry point (looked for: $($EntryCandidates -join ', '))"
    Write-Err "edit `$EntryCandidates at the top of this script"
    exit 1
}

# 5. ask to run
Write-Host ""
$answer = Read-Host "setup done. run meowbot now? [Y/n]"
if ($answer -eq "" -or $answer -match "^[yY]") {
    Write-Step "starting meowbot ($Entry). ctrl+c to stop"
    uv run python $Entry
} else {
    Write-Host "`naight, not running it. start it later with: uv run python $Entry" -ForegroundColor Cyan
}