$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "[TMAP 도보 경로]"
python -X utf8 -m plan_b_api.cli walking-route `
  --start-x 126.9767 --start-y 37.5760 `
  --end-x 127.0095 --end-y 37.5665 `
  --end-name "DDP"

Write-Host "`n[TMAP 자동차 경로]"
python -X utf8 -m plan_b_api.cli car-route `
  --start-x 126.9767 --start-y 37.5760 `
  --end-x 127.0095 --end-y 37.5665 `
  --end-name "DDP"
