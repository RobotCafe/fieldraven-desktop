# Run once to create the FieldRaven desktop shortcut.
# Usage: right-click → "Run with PowerShell"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$LaunchScript = "$ProjectDir\launch.ps1"
$Shortcut     = "$env:USERPROFILE\Desktop\FieldRaven.lnk"

# Icon — prefer Chrome (since that's what we open), fall back to shell32 globe
$ChromePaths = @(
    "$env:PROGRAMFILES\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "${env:PROGRAMFILES(x86)}\Google\Chrome\Application\chrome.exe"
)
$Chrome = $ChromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
$IconPath = if ($Chrome) { "$Chrome,0" } else { "$env:SystemRoot\System32\shell32.dll,14" }

$WS  = New-Object -ComObject WScript.Shell
$Lnk = $WS.CreateShortcut($Shortcut)
$Lnk.TargetPath       = "powershell.exe"
$Lnk.Arguments        = "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$LaunchScript`""
$Lnk.WorkingDirectory = $ProjectDir
$Lnk.Description      = "FieldRaven Desktop"
$Lnk.IconLocation     = $IconPath
$Lnk.Save()

Write-Host "Shortcut created: $Shortcut" -ForegroundColor Green
Write-Host "Double-click it to launch FieldRaven." -ForegroundColor Cyan
