[CmdletBinding()]
param(
    [string]$HostName = "103.242.2.226",
    [string]$UserName = "root",
    [string]$HostKey = "SHA256:G/9h52M5XCB2NRrXSAikn5xChgDOFOJkaQiDiPuTy08",
    [string]$ReleaseRoot = "/tmp/verigo-release",
    [SecureString]$Password
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$plink = "${env:ProgramFiles}\PuTTY\plink.exe"
$pscp = "${env:ProgramFiles}\PuTTY\pscp.exe"
if (!(Test-Path $plink) -or !(Test-Path $pscp)) {
    throw "PuTTY plink.exe and pscp.exe are required."
}

if (!$Password) {
    $Password = Read-Host "SSH password" -AsSecureString
}
$passwordBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
try {
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordBstr)
    $version = (git -C $repoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch "^[0-9a-f]{40}$") {
        throw "The current folder must be a Git repository with a valid HEAD commit."
    }

    $archive = Join-Path $env:TEMP "verigo-$version.tar.gz"
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    git -C $repoRoot archive --format=tar.gz --output=$archive HEAD
    if ($LASTEXITCODE -ne 0) { throw "Could not create the release archive." }

    $remote = "$UserName@$HostName"
    & $plink -batch -hostkey $HostKey -pw $plainPassword $remote "rm -rf $ReleaseRoot; mkdir -p $ReleaseRoot"
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the remote release directory." }
    & $pscp -batch -hostkey $HostKey -pw $plainPassword $archive "${remote}:$ReleaseRoot/release.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "Could not upload the release archive." }
    & $plink -batch -hostkey $HostKey -pw $plainPassword $remote "tar -xzf $ReleaseRoot/release.tar.gz -C $ReleaseRoot; printf '%s\n' $version > $ReleaseRoot/.verigo-release; VERIGO_RELEASE_DIR=$ReleaseRoot bash $ReleaseRoot/deploy/release.sh"
    if ($LASTEXITCODE -ne 0) { throw "Release failed; the server rollback was attempted." }
} finally {
    if ($passwordBstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordBstr)
    }
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
}
