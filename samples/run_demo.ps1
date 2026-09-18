# run_demo.ps1 — POST the sample to /optimize-energy and pretty-print the response.
# Usage (from the gridwise/ folder):
#   .\samples\run_demo.ps1
# Or to override the JSON file:
#   .\samples\run_demo.ps1 -SampleFile ".\samples\request_multi.json"

param(
    [string]$SampleFile = ".\samples\request_solar_reduction.json"
)

if (-not (Test-Path $SampleFile)) {
    Write-Error "Sample file not found: $SampleFile"
    exit 1
}

$uri  = "http://localhost:8000/optimize-energy"
$body = Get-Content -Raw -Path $SampleFile

try {
    $resp = Invoke-WebRequest -Method Post -Uri $uri `
        -ContentType "application/json; charset=utf-8" `
        -Body $body `
        -UseBasicParsing
    $resp.Content | ConvertFrom-Json | ConvertTo-Json -Depth 12
}
catch {
    # Surface the response body for 4xx errors so you see the JSON error envelope
    if ($_.Exception.Response) {
        $stream = $_.Exception.Response.GetResponseStream()
        $reader = New-Object System.IO.StreamReader($stream)
        $text   = $reader.ReadToEnd()
        try   { $text | ConvertFrom-Json | ConvertTo-Json -Depth 8 }
        catch { $text }
    } else {
        Write-Error $_.Exception.Message
    }
    exit 1
}
