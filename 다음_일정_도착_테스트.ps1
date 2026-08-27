$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

python -X utf8 -m plan_b_api.cli recommend-nearby-optimized `
  --x 126.9767 --y 37.5760 `
  --arrival 2026-08-04T14:00 `
  --weather-severity 0.2 `
  --next-x 127.0095 --next-y 37.5665 `
  --next-arrival 2026-08-04T17:00 `
  --next-title "동대문디자인플라자 예약" `
  --visit-minutes 60 `
  --schedule-buffer-minutes 10 `
  --max-walking-minutes 15 `
  --max-transport-minutes 30
