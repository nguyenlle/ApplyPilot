# Run from this repository: . .\use-local.ps1
$env:APPLYPILOT_DIR = Join-Path $PSScriptRoot '.private\state'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot '.private\browsers'
$env:PYTHONUTF8 = '1'
$env:npm_config_cache = Join-Path $PSScriptRoot '.private\npm-cache'
$env:PATH = (Join-Path $PSScriptRoot '.venv\Scripts') + ';' + (Join-Path $env:USERPROFILE '.local\bin') + ';' + $env:PATH
