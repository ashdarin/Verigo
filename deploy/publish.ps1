[CmdletBinding()]
param(
    [string]$HostName = "103.242.2.226",
    [string]$UserName = "verigo-deploy",
    [string]$ReleaseRoot = "/tmp/verigo-release",
    [switch]$Maintenance,
    [string]$KeyPath = (Join-Path $env:USERPROFILE ".ssh\verigo_deploy_ed25519"),
    [string]$KnownHostsPath = (Join-Path $env:USERPROFILE ".ssh\verigo_known_hosts")
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$ssh = "${env:ProgramFiles}\Git\usr\bin\ssh.exe"
$scp = "${env:ProgramFiles}\Git\usr\bin\scp.exe"
if (!(Test-Path $ssh) -or !(Test-Path $scp)) {
    throw "Git for Windows SSH and SCP are required."
}
if (!(Test-Path $KeyPath)) {
    throw "Deployment private key was not found: $KeyPath"
}
if (!(Test-Path $KnownHostsPath)) {
    throw "Verified known-hosts file was not found: $KnownHostsPath"
}

$remote = "$UserName@$HostName"
$sshOptions = @(
    "-F", "/dev/null", "-i", $KeyPath,
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "UserKnownHostsFile=$KnownHostsPath"
)

try {
    $version = (git -C $repoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch "^[0-9a-f]{40}$") {
        throw "The current folder must be a Git repository with a valid HEAD commit."
    }

    $archive = Join-Path $env:TEMP "verigo-$version.tar.gz"
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    git -C $repoRoot archive --format=tar.gz --output=$archive HEAD
    if ($LASTEXITCODE -ne 0) { throw "Could not create the release archive." }

    & $ssh @sshOptions $remote "sudo -n rm -rf -- $ReleaseRoot; sudo -n install -d -m 700 -o $UserName -g $UserName $ReleaseRoot"
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the remote release directory." }
    & $scp @sshOptions $archive "${remote}:$ReleaseRoot/release.tar.gz"
    if ($LASTEXITCODE -ne 0) { throw "Could not upload the release archive." }
    $maintenanceEnv = if ($Maintenance) { "VERIGO_DEPLOY_MAINTENANCE=true " } else { "" }
    & $ssh @sshOptions $remote "tar -xzf $ReleaseRoot/release.tar.gz -C $ReleaseRoot; printf '%s\n' $version > $ReleaseRoot/.verigo-release; sudo -n env ${maintenanceEnv}VERIGO_RELEASE_DIR=$ReleaseRoot bash $ReleaseRoot/deploy/release.sh"
    if ($LASTEXITCODE -ne 0) { throw "Release failed; the server rollback was attempted." }
} finally {
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
}
