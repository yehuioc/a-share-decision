$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path $projectRoot '.tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        python -m venv --system-site-packages .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 环境创建失败' }
    }
    & '.\.venv\Scripts\python.exe' -m pip install --disable-pip-version-check --cache-dir .tmp/pip-cache -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
    & '.\.venv\Scripts\python.exe' -m app.cli init
} finally { Pop-Location }
