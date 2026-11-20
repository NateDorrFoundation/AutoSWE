# setup.ps1 — autoSWE Interactive Setup Wizard (Windows)
#
# Runs the first-run setup wizard to configure credentials and settings.
# Creates config/repos.json and config/autoswe.env.
#
# Usage:
#   .\setup.ps1          # guided first-run setup
#   .\setup.ps1 --force  # overwrite existing config without prompting
#   .\setup.ps1 -NoWarmup   # skip the pi-mcp cold-start warm-up

$ErrorActionPreference = "Stop"

$AUTOSWE_DIR = if ($env:AUTOSWE_DIR) { $env:AUTOSWE_DIR } else { $PSScriptRoot }
$AUTOSWE_PY  = Join-Path $AUTOSWE_DIR "autoswe.py"
$VENV_PY     = Join-Path $AUTOSWE_DIR ".venv\Scripts\python.exe"

$PYTHON = if (Test-Path $VENV_PY) { $VENV_PY } else { "python" }

$NoWarmup = $false
$SetupArgs = @()
foreach ($a in $args) {
    if ($a -eq "-NoWarmup") { $NoWarmup = $true }
    else { $SetupArgs += $a }
}

# Run the setup wizard, preserving any user-supplied flags (e.g. -Force).
& $PYTHON $AUTOSWE_PY setup @SetupArgs

# pi Phase 4 cold-start warm-up (docs/autoswe/PLAN-pi-mcp.md): populate each pi
# profile's mcp-cache.json so the first real run uses the direct
# mcp__autoswe_comment_* tools. Skips cleanly when no pi profile with an
# agent_dir is configured, so non-pi setups are unaffected. Skipped with
# -NoWarmup.
if (-not $NoWarmup) {
    & $PYTHON $AUTOSWE_PY warmup
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "pi-mcp warm-up did not complete (first pi run will use proxy tool shapes)"
    }
}

# Antigravity CLI check (Phase 6 §6.2): NO install step — agy is a user-level
# install that needs interactive auth once. Warn-only: never fails setup. There
# is deliberately no warm-up call for agy (spike M5: eager MCP tools work on a
# cold cache; unlike pi, no warm-up is needed).
$HarnessesJson = Join-Path $AUTOSWE_DIR "config\harnesses.json"
if (Test-Path $HarnessesJson) {
    $AgyProfiles = @()
    try {
        $Harnesses = Get-Content $HarnessesJson -Raw | ConvertFrom-Json
        foreach ($prop in $Harnesses.PSObject.Properties) {
            if ($prop.Name -notmatch '^_' -and
                "$($prop.Value.backend)".ToLower() -eq 'antigravity') {
                $AgyProfiles += $prop.Value
            }
        }
    } catch { }
    if ($AgyProfiles.Count -gt 0) {
        $AgyBin = $null
        foreach ($prof in $AgyProfiles) {
            $cp = "$($prof.cli_path)".Trim()
            if ($cp) {
                if ($cp.StartsWith('~')) { $cp = $cp -replace '^~', $HOME }
                $AgyBin = $cp
                break
            }
        }
        if (-not $AgyBin) {
            $cmd = Get-Command agy -ErrorAction SilentlyContinue
            if ($cmd) { $AgyBin = $cmd.Source }
        }
        if ($AgyBin -and (Get-Command $AgyBin -ErrorAction SilentlyContinue)) {
            $AgyVer = try { (& $AgyBin --version 2>&1 | Select-Object -First 1) } catch { $null }
            Write-Host "  antigravity backend: found $AgyBin$(if ($AgyVer) { " ($AgyVer)" })"
        } else {
            Write-Host "  antigravity backend: no agy binary found (PATH or profile cli_path) — install the Antigravity CLI first"
        }
        Write-Host "  antigravity backend: run ``agy`` interactively once to authenticate (headless dispatch reuses that session; see docs/autoswe/harnesses.md)"
    }
}
