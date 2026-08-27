$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

python -X utf8 -m plan_b_api.cli place-search "경복궁" `
  --center-x 126.9767 --center-y 37.5760 --count 5
