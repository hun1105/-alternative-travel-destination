$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$cacheDb = ".cache\plan_b_api.sqlite3"

Write-Host "[1] create-trip-plan"
$created = python -X utf8 -m plan_b_api.cli create-trip-plan ".\examples\trip_plan_example.json" --cache-db $cacheDb | ConvertFrom-Json
$created | ConvertTo-Json -Depth 10
$tripId = $created.trip_id
Write-Host "trip_id: $tripId"

Write-Host "[2] get-trip-plan"
python -X utf8 -m plan_b_api.cli get-trip-plan --trip-id $tripId --cache-db $cacheDb

Write-Host "[3] replace-trip-schedule"
python -X utf8 -m plan_b_api.cli replace-trip-schedule --trip-id $tripId ".\examples\trip_schedule_replacement_example.json" --cache-db $cacheDb

Write-Host "[4] get-trip-plan after replace"
python -X utf8 -m plan_b_api.cli get-trip-plan --trip-id $tripId --cache-db $cacheDb
