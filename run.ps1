<#
  Set up and start FHIR Record Retrieval on Windows.

    .\run.ps1            # install if needed, then serve http://127.0.0.1:8000
    .\run.ps1 -Dev       # auto-reload on code changes
    .\run.ps1 -Mock      # also start the offline mock FHIR server on :9000
    .\run.ps1 -Test      # run the test suite
    .\run.ps1 -Check     # check this machine can reach Cerner and Azure OpenAI

  Host and port come from APP_HOST / APP_PORT (environment, else .env; defaults 127.0.0.1 / 8000).
  If PowerShell blocks scripts, use run.cmd instead.
#>
param([switch]$Dev, [switch]$Mock, [switch]$Test, [switch]$Check)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Pick a Python 3.10+ (the py launcher first, then python on PATH).
$python = $null
foreach ($candidate in @("py -3.13", "py -3.12", "py -3.11", "py -3.10", "py -3", "python")) {
    $exe, $arg = $candidate.Split(" ", 2)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $ok = & $exe @($arg | Where-Object { $_ }) -c "import sys; print(sys.version_info >= (3, 10))" 2>$null
        if ($ok -eq "True") { $python = $candidate; break }
    } catch { }
}
if (-not $python) { throw "Python 3.10 or newer not found. Install it from https://www.python.org/downloads/ (tick 'Add to PATH')." }

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment with $python ..."
    $exe, $arg = $python.Split(" ", 2)
    & $exe @($arg | Where-Object { $_ }) -m venv .venv
}

# Reinstall only when requirements.txt changed.
$stamp = Join-Path $PSScriptRoot ".venv\requirements.stamp"
$hash = (Get-FileHash requirements.txt).Hash
if (-not (Test-Path $stamp) -or (Get-Content $stamp) -ne $hash) {
    Write-Host "Installing requirements ..."
    & $venvPython -m pip install --disable-pip-version-check -q -r requirements.txt
    Set-Content -Path $stamp -Value $hash
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example - add AOAI_ENDPOINT and AOAI_API_KEY to use Azure OpenAI."
}

if ($Test) { & $venvPython -m pytest -q tests; exit $LASTEXITCODE }
if ($Check) { & $venvPython -m dev.check; exit $LASTEXITCODE }

# Read APP_HOST / APP_PORT from .env (ignoring comments).
$envVars = @{}
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*([^#]*)') { $envVars[$Matches[1]] = $Matches[2].Trim() }
}
# Environment variables win over .env, e.g.  $env:APP_PORT = "8080"; .\run.ps1
$appHost = if ($env:APP_HOST) { $env:APP_HOST } elseif ($envVars["APP_HOST"]) { $envVars["APP_HOST"] } else { "127.0.0.1" }
$appPort = if ($env:APP_PORT) { $env:APP_PORT } elseif ($envVars["APP_PORT"]) { $envVars["APP_PORT"] } else { "8000" }

if ($Mock) {
    Write-Host "Starting the offline mock FHIR server on http://127.0.0.1:9000 (set FHIR_BASE=http://localhost:9000/r4/mock in .env)"
    Start-Process -FilePath $venvPython -ArgumentList "-m", "uvicorn", "dev.mock_fhir:app", "--port", "9000" -NoNewWindow
}

$uvicornArgs = @("-m", "uvicorn", "agent.app:app", "--host", $appHost, "--port", $appPort)
if ($Dev) { $uvicornArgs += "--reload" }
Write-Host "Open http://$(if ($appHost -eq '0.0.0.0') { 'localhost' } else { $appHost }):$appPort   (Ctrl+C to stop)"
& $venvPython @uvicornArgs
