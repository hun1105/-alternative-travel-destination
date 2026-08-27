$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

python -X utf8 -m plan_b_api.cli seoul-crowd --x 126.9767 --y 37.5760
