param(
    [switch]$SkipLatestVersionCheck
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = "python"
$MetadataJson = & $PythonExe -c "import json; from app_metadata import APP_TITLE, APP_VERSION, APP_PUBLISHER; print(json.dumps({'title': APP_TITLE, 'version': APP_VERSION, 'publisher': APP_PUBLISHER}))"
if ($LASTEXITCODE -ne 0 -or -not $MetadataJson) {
    throw "Could not load app metadata from app_metadata.py"
}
$AppMetadata = $MetadataJson | ConvertFrom-Json
$AppName = [string]$AppMetadata.title
$AppVersion = [string]$AppMetadata.version
$AppPublisher = [string]$AppMetadata.publisher
$AppExeName = "$AppName.exe"
$FreshVersionedSourceDir = Join-Path $ProjectRoot "dist-fresh\$AppVersion\$AppName"
$FreshSourceDir = Join-Path $ProjectRoot "dist-fresh\$AppName"
$DefaultSourceDir = Join-Path $ProjectRoot "dist\$AppName"
$AppSourceDir = if (Test-Path $FreshVersionedSourceDir) {
    "dist-fresh\$AppVersion\$AppName"
} elseif (Test-Path $FreshSourceDir) {
    "dist-fresh\$AppName"
} else {
    "dist\$AppName"
}
$AppSourceDir = $AppSourceDir -replace "\\", "/"
$LatestJsonPath = Join-Path $ProjectRoot "latest.json"
$PossiblePaths = @(
    (Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
)

$IsccPath = $null
foreach ($Candidate in $PossiblePaths) {
    if ($Candidate -and (Test-Path -LiteralPath $Candidate)) {
        $IsccPath = [string]$Candidate
        break
    }
}

if (-not $IsccPath) {
    throw "Inno Setup Compiler (ISCC.exe) was not found. Install Inno Setup 6 first."
}

if (-not $SkipLatestVersionCheck -and (Test-Path $LatestJsonPath)) {
    $LatestManifest = Get-Content $LatestJsonPath -Raw | ConvertFrom-Json
    if ([string]$LatestManifest.version -ne $AppVersion) {
        throw "latest.json version ($($LatestManifest.version)) does not match app version ($AppVersion)."
    }
}

$FullSourceDir = (Resolve-Path (Join-Path $ProjectRoot $AppSourceDir)).Path
$DriveRoot = [System.IO.Path]::GetPathRoot($FullSourceDir)
$ShortJunction = Join-Path $DriveRoot ("mt-inno-" + [System.Diagnostics.Process]::GetCurrentProcess().Id)
$InnoSourceDir = $AppSourceDir
$CreatedJunction = $false

try {
    if ($FullSourceDir.Length -gt 40) {
        try {
            if (Test-Path -LiteralPath $ShortJunction) {
                [System.IO.Directory]::Delete($ShortJunction, $false)
            }
            New-Item -ItemType Junction -Path $ShortJunction -Target $FullSourceDir | Out-Null
            $CreatedJunction = $true
            $InnoSourceDir = $ShortJunction
        }
        catch {
            $CreatedJunction = $false
            $InnoSourceDir = $AppSourceDir
        }
    }

    & "$IsccPath" `
        "/DMyAppName=$AppName" `
        "/DMyAppVersion=$AppVersion" `
        "/DMyAppPublisher=$AppPublisher" `
        "/DMyAppExeName=$AppExeName" `
        "/DMyAppSourceDir=$InnoSourceDir" `
        (Join-Path $ProjectRoot "installer.iss")
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup build failed with exit code $LASTEXITCODE"
    }
}
finally {
    if ($CreatedJunction -and (Test-Path -LiteralPath $ShortJunction)) {
        [System.IO.Directory]::Delete($ShortJunction, $false)
    }
}

Write-Host ""
Write-Host "Installer created in:"
Write-Host "  $(Join-Path $ProjectRoot "installer-dist")"
