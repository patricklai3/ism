param (
    [switch]$Delete = $false,
    [switch]$Checksum = $false,
    [switch]$PruneStale = $false,
    [string]$Source = "",
    [string]$Destination = "",
    [string]$Distro = "",
    [switch]$AllowWorktreeOnMain = $false
)

$ErrorActionPreference = "Stop"

function Test-PrimaryTrunkCheckout {
    param ([string]$CheckPath)
    $PrimaryRoot = if ($env:ISM_PRIMARY_REPO) { $env:ISM_PRIMARY_REPO } else { "D:\ism" }
    $ExpectedNorm = [System.IO.Path]::GetFullPath($PrimaryRoot).TrimEnd('\')

    $TopLevel = (git -C $CheckPath rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $TopLevel) { return $false, "path '$CheckPath' is not inside a git repository" }
    $TopNorm = [System.IO.Path]::GetFullPath($TopLevel.Trim().Replace('/', '\')).TrimEnd('\')
    if ($TopNorm -ine $ExpectedNorm) { return $false, "repo root '$TopNorm' is not primary checkout '$ExpectedNorm'" }

    $GitDir = (git -C $TopNorm rev-parse --path-format=absolute --git-dir 2>$null)
    $GitCommonDir = (git -C $TopNorm rev-parse --path-format=absolute --git-common-dir 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $GitDir -or -not $GitCommonDir) { return $false, "unable to inspect git worktree state" }
    $GitDirNorm = [System.IO.Path]::GetFullPath($GitDir.Trim().Replace('/', '\')).TrimEnd('\')
    $CommonDirNorm = [System.IO.Path]::GetFullPath($GitCommonDir.Trim().Replace('/', '\')).TrimEnd('\')
    if ($GitDirNorm -ine $CommonDirNorm) { return $false, "linked git worktree detected" }

    $Branch = (git -C $TopNorm rev-parse --abbrev-ref HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $Branch) { return $false, "unable to determine active git branch" }
    if ($Branch.Trim() -cne "main") { return $false, "active branch is '$($Branch.Trim())' (expected 'main')" }

    return $true, "ok"
}

function ConvertTo-WslPath {
    param ([string]$WinPath, [string]$DistroName)
    $norm = $WinPath.Replace('\', '/')
    if ($norm -match '^([A-Za-z]):/(.*)$') {
        return "/mnt/$($Matches[1].ToLowerInvariant())/$($Matches[2].TrimEnd('/'))"
    }
    $resolved = (wsl -d $DistroName --exec wslpath -u "$norm").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $resolved) {
        Write-Error "Failed to convert '$norm' to WSL path."
        exit 1
    }
    return $resolved
}

$DefaultDistro = if ($env:ISM_WSL_DISTRO) { $env:ISM_WSL_DISTRO } else { "Ubuntu" }
$TargetDistro = if ($Distro) { $Distro } else { $DefaultDistro }

$PrimaryBenchDest = "/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/"
$DefaultDestination = if ($env:ISM_WSL_DEST) { $env:ISM_WSL_DEST } else { $PrimaryBenchDest }
$WslDestination = if ($Destination) { $Destination } else { $DefaultDestination }
if (-not $WslDestination.EndsWith("/")) { $WslDestination += "/" }

$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$CandidateSource = if ($Source) { $Source } else { Join-Path $RepoRoot "ism-frappe" }

if (($WslDestination -eq $PrimaryBenchDest) -and (-not $AllowWorktreeOnMain) -and ($env:ISM_ALLOW_WORKTREE_MAIN -ne "1")) {
    $CheckTargets = @($RepoRoot)
    if ($Source -or (Test-Path $CandidateSource)) {
        $CheckTargets += $CandidateSource
    }
    foreach ($CheckTarget in $CheckTargets) {
        $Ok, $Why = Test-PrimaryTrunkCheckout -CheckPath $CheckTarget
        if (-not $Ok) {
            Write-Error "Refusing to sync to primary bench '$PrimaryBenchDest' ($Why). Run scripts\test-in-slot.ps1 for isolated slot testing, or pass -AllowWorktreeOnMain outside agent sessions."
            exit 1
        }
    }
}

$LocalSource = (Get-Item $CandidateSource).FullName
$NormalizedSource = $LocalSource.Replace('\', '/')
$WindowsSource = (ConvertTo-WslPath -WinPath $LocalSource -DistroName $TargetDistro) + "/"

$Excludes = @(
    "--exclude=.git",
    "--exclude=.codegraph",
    "--exclude=node_modules",
    "--exclude=__pycache__",
    "--exclude=*.pyc",
    "--exclude=.pytest_cache",
    "--exclude=.venv",
    "--exclude=.idea",
    "--exclude=.vscode"
)

$RsyncFlags = if ($Checksum) { "-rtvc" } else { "-rt --modify-window=1" }
$DeleteFlag = if ($Delete) { "--delete" } else { "" }

$PrunePreCmd = ""
if ($PruneStale -and ($WslDestination -eq $PrimaryBenchDest)) {
    $StaleCandidates = @(".git", ".codegraph", "ism-agent-os", "scripts")
    $ToRemove = @()
    foreach ($Name in $StaleCandidates) {
        if (-not (Test-Path (Join-Path $LocalSource $Name))) {
            $ToRemove += "'${WslDestination}${Name}'"
        }
    }
    if ($ToRemove.Count -gt 0) {
        $PrunePreCmd = "rm -rf $($ToRemove -join ' ') && "
    }
}

$Command = "${PrunePreCmd}rsync $RsyncFlags $DeleteFlag $($Excludes -join ' ') '$WindowsSource' '$WslDestination'"
$CmdB64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Command))

Write-Host "[sync-to-wsl] Syncing $NormalizedSource -> WSL2 ($TargetDistro`:$WslDestination)..." -ForegroundColor Cyan
wsl -d $TargetDistro --exec python3 -c 'import base64,subprocess,sys;sys.exit(subprocess.call(sys.argv[2:4]+[base64.b64decode(sys.argv[1]).decode()]))' $CmdB64 bash -c

if ($LASTEXITCODE -eq 0) {
    Write-Host "[sync-to-wsl] Complete." -ForegroundColor Green
} else {
    Write-Host "[sync-to-wsl] Error during rsync (exit code $LASTEXITCODE)." -ForegroundColor Red
    exit $LASTEXITCODE
}
