$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Spec = Join-Path $Root "residential_ip_manager.spec"
$Output = Join-Path $Root "dist\ResidentialIPManager.exe"

if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
    $Python = $VenvPython
}
else {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

Push-Location $Root
try {
    & $Python "scripts\build_assets.py"
    if ($LASTEXITCODE -ne 0) {
        throw "Icon generation failed."
    }

    & $Python -m PyInstaller --noconfirm --clean $Spec
    if ($LASTEXITCODE -ne 0) {
        throw "EXE build failed."
    }

    $File = Get-Item -LiteralPath $Output
    $SizeMb = [Math]::Round($File.Length / 1MB, 2)
    Write-Host "Build complete: $($File.FullName) ($SizeMb MB)"
}
finally {
    Pop-Location
}
