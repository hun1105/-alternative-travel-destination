$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

python -X utf8 -m plan_b_api.scenario_runner
