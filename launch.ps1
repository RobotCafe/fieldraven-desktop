$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Port   = 8081
$Url    = "http://localhost:$Port"
$Python = "C:\Users\DenmanNic\AppData\Local\Programs\Python\Python313\python.exe"

# If server is already up, just open Chrome
$already = $false
try {
    Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop | Out-Null
    $already = $true
} catch {}

if (-not $already) {
    Start-Process $Python `
        -ArgumentList "-X utf8 `"$ProjectDir\main.py`" --no-browser" `
        -WorkingDirectory $ProjectDir `
        -WindowStyle Minimized

    $ready = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop | Out-Null
            $ready = $true
            break
        } catch {}
    }
    if (-not $ready) { exit 1 }
}

$ChromePaths = @(
    "$env:PROGRAMFILES\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "${env:PROGRAMFILES(x86)}\Google\Chrome\Application\chrome.exe"
)
$Chrome = $ChromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($Chrome) {
    Start-Process $Chrome "--app=$Url --new-window --window-size=1440,900"
} else {
    Start-Process $Url
}
