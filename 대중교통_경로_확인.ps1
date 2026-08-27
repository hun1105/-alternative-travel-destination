$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

python -X utf8 -m plan_b_api.cli seoul-transit-route `
  --start-x 126.9767 --start-y 37.5760 `
  --end-x 127.0095 --end-y 37.5665
