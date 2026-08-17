[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("shanghai-app", "hong-kong-edge-worker")]
    [string]$Role,
    [string]$HostName,
    [string]$UserName = "verigo-deploy",
    [string]$ReleaseRoot = "/tmp/verigo-release",
    [switch]$Maintenance,
    [string]$KeyPath = (Join-Path $env:USERPROFILE ".ssh\verigo_deploy_ed25519"),
    [string]$KnownHostsPath = (Join-Path $env:USERPROFILE ".ssh\verigo_known_hosts")
)

$ErrorActionPreference = "Stop"
$roleHosts = @{
    "shanghai-app" = "101.34.212.199"
    "hong-kong-edge-worker" = "103.242.2.226"
}
if (!$HostName) {
    $HostName = $roleHosts[$Role]
}
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
    "-o", "ConnectTimeout=15",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "UserKnownHostsFile=$KnownHostsPath"
)

function Invoke-NativeWithRetry {
    param(
        [Parameter(Mandatory)]
        [scriptblock]$Operation,
        [Parameter(Mandatory)]
        [string]$Description,
        [ValidateRange(1, 8)]
        [int]$MaxAttempts = 5
    )

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        & $Operation
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            return
        }
        if ($attempt -eq $MaxAttempts) {
            throw "$Description failed after $MaxAttempts attempts (exit code $exitCode)."
        }
        $delaySeconds = [Math]::Min(15, [int][Math]::Pow(2, $attempt))
        Write-Warning "$Description failed on attempt $attempt; retrying in $delaySeconds seconds."
        Start-Sleep -Seconds $delaySeconds
    }
}

try {
    $version = (git -C $repoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch "^[0-9a-f]{40}$") {
        throw "The current folder must be a Git repository with a valid HEAD commit."
    }
    $uncommitted = git -C $repoRoot status --porcelain
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect the Git working tree." }
    if ($uncommitted) {
        throw "Deployment requires a clean Git working tree because the release archive contains HEAD only. Commit the intended changes before publishing."
    }

    $archive = Join-Path $env:TEMP "verigo-$version.tar.gz"
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    git -C $repoRoot archive --format=tar.gz --output=$archive HEAD
    if ($LASTEXITCODE -ne 0) { throw "Could not create the release archive." }

    Invoke-NativeWithRetry -Description "Prepare remote release directory" -Operation {
        & $ssh @sshOptions $remote "rm -rf -- $ReleaseRoot; install -d -m 700 $ReleaseRoot"
    }
    Invoke-NativeWithRetry -Description "Upload release archive" -Operation {
        & $scp @sshOptions $archive "${remote}:$ReleaseRoot/release.tar.gz"
    }
    $maintenanceValue = if ($Maintenance) { "true" } else { "false" }
    Invoke-NativeWithRetry -Description "Apply release" -Operation {
        & $ssh @sshOptions $remote "tar -xzf $ReleaseRoot/release.tar.gz -C $ReleaseRoot; printf '%s\n' $version > $ReleaseRoot/.verigo-release; sudo -n /usr/local/sbin/verigo-apply-release $Role $ReleaseRoot $maintenanceValue"
    }
} finally {
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
}
