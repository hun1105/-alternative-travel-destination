$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "Plan B JSON API 서버를 시작합니다."
Write-Host "주소: http://127.0.0.1:8000"
Write-Host "종료: Ctrl+C"

python -X utf8 -m plan_b_api.server --host 127.0.0.1 --port 8000
