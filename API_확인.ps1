$ErrorActionPreference = "Stop"

Write-Host "[서버 상태]"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" |
    ConvertTo-Json -Depth 5

$requestBody = @{
    longitude = 126.9767
    latitude = 37.5760
    priorities = @(2, 4, 6)
    radius = 3000
    search_rows = 20
    eligible_count = 3
    max_detail_calls = 12
    budget = 20000
    party_size = 2
    route_mode = "tmap"
} | ConvertTo-Json

Write-Host "`n[추천 결과]"
Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8000/recommendations" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($requestBody)) |
    ConvertTo-Json -Depth 10
