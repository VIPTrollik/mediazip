# G-auto gate (PLAN.md §12): lint, format, types, tests + coverage. Run from the repo root.
$ErrorActionPreference = "Stop"
$py = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$covModules = @("media", "naming", "stats", "store") | ForEach-Object { Join-Path "src\zipbot" "$_.py" } | Where-Object { Test-Path $_ }

function Step($name, [scriptblock]$cmd) {
    Write-Host "== $name"
    & $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $name" -ForegroundColor Red; exit $LASTEXITCODE }
}

Step "ruff check" { & $py -m ruff check src tests }
Step "ruff format" { & $py -m ruff format --check src tests }
Step "pyright" { & $py -m pyright }
Step "pytest" { & $py -m pytest --cov --cov-report=term-missing:skip-covered }
if ($covModules) {
    $include = ($covModules -join ",")
    Step "coverage >= 90% ($include)" { & $py -m coverage report --include=$include --fail-under=90 }
}
# The lock is the full closure for the 3.12 container, so audit it as-is (no re-resolve).
Step "pip-audit (runtime lock)" { & $py -m pip_audit -r requirements.lock --disable-pip --no-deps --progress-spinner off }
Write-Host "G-auto passed" -ForegroundColor Green
