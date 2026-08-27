$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

python -X utf8 -m plan_b_api.cli validate-trip-plan `
  ".\examples\trip_plan_example.json"
