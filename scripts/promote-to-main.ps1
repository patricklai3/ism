param (
    [switch]$AllowDirty = $false,
    [switch]$SkipSmokeTest = $false,
    [switch]$Fast = $false,
    [ValidateSet("bg-warm", "lazy", "eager", "none")]
    [string]$SlotSync = "bg-warm",
    [switch]$ForceMigrate = $false,
    [switch]$ForceGolden = $false,
    [switch]$RebuildBase = $false,
    [switch]$RunAgentOsTests = $false,
    [string]$SmokeModule = "ism.tests.test_simulation_foundation,ism.tests.test_estimation_math",
    [string]$Distro = ""
)

$ErrorActionPreference = "Stop"

if ($Fast) {
    $SkipSmokeTest = $true
    if (-not $PSBoundParameters.ContainsKey("SlotSync")) {
        $SlotSync = "lazy"
    }
}

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

function Ensure-LfNoBom {
    param ([string]$FilePath)
    if (-not (Test-Path $FilePath)) { return }
    $bytes = [System.IO.File]::ReadAllBytes($FilePath)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    $hasCr = ($bytes -contains 0x0D)
    if ($hasBom -or $hasCr) {
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        if ($hasBom) { $text = $text.TrimStart([char]0xFEFF) }
        $text = $text.Replace("`r`n", "`n").Replace("`r", "`n")
        [System.IO.File]::WriteAllBytes($FilePath, ([System.Text.UTF8Encoding]::new($false)).GetBytes($text))
    }
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

$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$Ok, $Why = Test-PrimaryTrunkCheckout -CheckPath $RepoRoot
if (-not $Ok) {
    Write-Error "Refusing promote-to-main outside canonical primary trunk ($Why)."
    exit 1
}

if (-not $AllowDirty) {
    $Dirty = (git -C $RepoRoot status --porcelain)
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to check git status in '$RepoRoot'."
        exit 1
    }
    if ($Dirty) {
        Write-Error "Working tree '$RepoRoot' has uncommitted changes. Commit/stash first or pass -AllowDirty."
        exit 1
    }
}

$HeadSha = ((git -C $RepoRoot rev-parse HEAD 2>$null) | Out-String).Trim()
if (-not $HeadSha) { $HeadSha = "unknown" }

$DefaultDistro = if ($env:ISM_WSL_DISTRO) { $env:ISM_WSL_DISTRO } else { "Ubuntu" }
$TargetDistro = if ($Distro) { $Distro } else { $DefaultDistro }

$WinCommonPath = (Resolve-Path (Join-Path $PSScriptRoot "wsl\slot-common.sh")).Path
Ensure-LfNoBom -FilePath $WinCommonPath
$WinProvisionPath = (Resolve-Path (Join-Path $PSScriptRoot "wsl\provision-slots.sh")).Path
Ensure-LfNoBom -FilePath $WinProvisionPath
$WslCommonPath = ConvertTo-WslPath -WinPath $WinCommonPath -DistroName $TargetDistro
$WslProvisionPath = ConvertTo-WslPath -WinPath $WinProvisionPath -DistroName $TargetDistro

$TotalSw = [System.Diagnostics.Stopwatch]::StartNew()
$SyncMs = 0
$PrimaryMs = 0
$StageAMs = 0
$GoldenMs = 0
$StageBMs = 0
$PrimaryMigrated = $false
$GoldenRebuilt = $false

# 1/5 Sync
$StepSw = [System.Diagnostics.Stopwatch]::StartNew()
Write-Host "[promote-to-main] 1/5 Syncing D:\ism\ism-frappe -> primary frappe-bench/apps/ism..." -ForegroundColor Cyan
& "$PSScriptRoot\sync-to-wsl.ps1" -Delete -PruneStale -Distro $TargetDistro
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$SyncMs = [int]$StepSw.ElapsedMilliseconds

# 2/5 + 3/5 Consolidated Primary Migrate/Clear-Cache + Stage A Smoke Check
$PrimaryStageScript = @'
set -euo pipefail
common_sh="$1"
force_mig="$2"
skip_smoke="$3"
source "$common_sh"

for ctr in "$CONTAINER_FRAPPE" "$CONTAINER_DB"; do
  running="$(docker inspect -f '{{.State.Running}}' "$ctr" 2>/dev/null || echo false)"
  if [[ "$running" != "true" ]]; then
    echo "[promote-to-main] Container '$ctr' is not running. Start the devcontainer first." >&2
    exit 1
  fi
done

ensure_dev_mariadb_tuning

t_prim_0="$(now_ms)"
cur_fp="$(compute_schema_fp "$WSL_PRIMARY_BENCH/apps/ism")"
prev_fp=""
if [[ -s "$PRIMARY_FP_FILE" ]]; then
  prev_fp="$(tr -d '[:space:]' < "$PRIMARY_FP_FILE")"
fi

container_path="PATH=/workspace/development/frappe-bench/env/bin:/home/frappe/.local/bin:/home/frappe/.nvm/versions/node/v24.13.0/bin:/home/frappe/.pyenv/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

if [[ "$force_mig" -eq 1 || -z "$prev_fp" || "$cur_fp" != "$prev_fp" ]]; then
  echo "[promote-to-main] 2/5 Schema changed (${prev_fp:0:8} -> ${cur_fp:0:8}); running bench migrate on development.localhost..."
  docker exec -w /workspace/development/frappe-bench "$CONTAINER_FRAPPE" env "$container_path" bench --site development.localhost migrate
  mkdir -p "$(dirname "$PRIMARY_FP_FILE")"
  tmp_pfp="${PRIMARY_FP_FILE}.tmp.$$"
  printf '%s\n' "$cur_fp" > "$tmp_pfp"
  mv -f "$tmp_pfp" "$PRIMARY_FP_FILE"
  prim_mig=1
else
  echo "[promote-to-main] 2/5 Schema unchanged (${cur_fp:0:8}); running fast clear-cache on development.localhost..."
  docker exec -w /workspace/development/frappe-bench "$CONTAINER_FRAPPE" env "$container_path" bench --site development.localhost clear-cache
  prim_mig=0
fi
t_prim_1="$(now_ms)"

t_stga_0="$(now_ms)"
if [[ "$skip_smoke" -eq 0 ]]; then
  echo "[promote-to-main] 3/5 Stage A: Read-only smoke check on development.localhost..."
  docker exec -i -w /workspace/development/frappe-bench/sites "$CONTAINER_FRAPPE" /workspace/development/frappe-bench/env/bin/python - <<'PY'
import json
import frappe

frappe.init(site="development.localhost", sites_path="/workspace/development/frappe-bench/sites")
frappe.connect()
try:
    import ism
    import ism.api.agent as agent
    settings = frappe.get_single("ISM Settings")
    status = agent.get_shop_floor_status()
    assert isinstance(status, dict), f"Unexpected shop floor status type: {type(status)}"
    print(json.dumps({"ok": True, "ism_file": ism.__file__, "company": settings.get("company"), "status_keys": sorted(status.keys())}))
finally:
    frappe.db.rollback()
    frappe.destroy()
PY
else
  echo "[promote-to-main] 3/5 Stage A skipped (-SkipSmokeTest)."
fi
t_stga_1="$(now_ms)"

printf '[promote-metrics] primary_migrated=%d primary_ms=%d stage_a_ms=%d\n' \
  "$prim_mig" "$(( t_prim_1 - t_prim_0 ))" "$(( t_stga_1 - t_stga_0 ))"
'@

$ForceMigVal = if ($ForceMigrate) { "1" } else { "0" }
$SkipSmokeVal = if ($SkipSmokeTest) { "1" } else { "0" }
$PrimaryB64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes(($PrimaryStageScript -replace "`r", "")))

wsl -d $TargetDistro --exec python3 -c 'import base64,subprocess,sys;sys.exit(subprocess.call(sys.argv[2:4]+[base64.b64decode(sys.argv[1]).decode()]+sys.argv[4:]))' $PrimaryB64 bash -c _ $WslCommonPath $ForceMigVal $SkipSmokeVal | ForEach-Object {
    $line = $_
    if ($line -match '^\[promote-metrics\] primary_migrated=(\d+) primary_ms=(\d+) stage_a_ms=(\d+)') {
        $PrimaryMigrated = ($Matches[1] -eq "1")
        $PrimaryMs = [int]$Matches[2]
        $StageAMs = [int]$Matches[3]
    } else {
        Write-Host $line
    }
}
if ($LASTEXITCODE -ne 0) {
    Write-Error "Primary migration or Stage A smoke check failed; aborting before updating golden DB snapshot."
    exit $LASTEXITCODE
}

# Optional fast Windows-side ism-agent-os / hook unit tests when touched
$ShouldRunAgentOs = $RunAgentOsTests
if (-not $ShouldRunAgentOs -and -not $SkipSmokeTest) {
    $TouchedHead = (git -C $RepoRoot diff --name-only HEAD~1..HEAD -- ism-agent-os .agents 2>$null)
    $TouchedDirty = (git -C $RepoRoot status --porcelain -- ism-agent-os .agents 2>$null)
    if ($TouchedHead -or $TouchedDirty) {
        $ShouldRunAgentOs = $true
    }
}
$AgentOsDir = Join-Path $RepoRoot "ism-agent-os"
if ($ShouldRunAgentOs -and (Test-Path (Join-Path $AgentOsDir "tests"))) {
    $StepSw.Restart()
    Write-Host "[promote-to-main] 3b/5 Running ism-agent-os & guardrail unit tests..." -ForegroundColor Cyan
    $AgentOsPy = Join-Path $AgentOsDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $AgentOsPy)) { $AgentOsPy = "python" }
    Push-Location $AgentOsDir
    try {
        & $AgentOsPy -m unittest discover -s tests
        $TestExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($TestExit -ne 0) {
        Write-Error "ism-agent-os unit tests failed; aborting promotion."
        exit $TestExit
    }
    $StageAMs += [int]$StepSw.ElapsedMilliseconds
}

# 4/5 Golden DB check/refresh & slot policy
$StepSw.Restart()
if ($SlotSync -eq "none") {
    Write-Host "[promote-to-main] 4/5 Golden DB & slot refresh skipped (-SlotSync none)." -ForegroundColor Yellow
} else {
    $Step4Sync = if ($SlotSync -eq "bg-warm" -and -not $SkipSmokeTest) { "lazy" } else { $SlotSync }
    Write-Host "[promote-to-main] 4/5 Checking/refreshing golden DB snapshot (slot_sync=$SlotSync)..." -ForegroundColor Cyan
    $ProvParams = @{
        RefreshGolden = $true
        SlotSync      = $Step4Sync
        Distro        = $TargetDistro
    }
    if ($ForceGolden) { $ProvParams["ForceGolden"] = $true }
    if ($RebuildBase) { $ProvParams["RebuildBase"] = $true }

    & "$PSScriptRoot\provision-bench-slots.ps1" @ProvParams | ForEach-Object {
        $line = $_
        if ($line -match 'golden_rebuilt=1') {
            $GoldenRebuilt = $true
        }
        Write-Host $line
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$GoldenMs = [int]$StepSw.ElapsedMilliseconds

# 5/5 Stage B Isolated Smoke Suite
$StepSw.Restart()
if (-not $SkipSmokeTest) {
    Write-Host "[promote-to-main] 5/5 Stage B: Running isolated smoke suite ($SmokeModule)..." -ForegroundColor Cyan
    & "$PSScriptRoot\test-in-slot.ps1" -Module $SmokeModule -Distro $TargetDistro
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    Write-Host "[promote-to-main] 5/5 Stage B skipped (-SkipSmokeTest)." -ForegroundColor Yellow
}
$StageBMs = [int]$StepSw.ElapsedMilliseconds
$TotalMs = [int]$TotalSw.ElapsedMilliseconds

# Record promote telemetry + trigger deferred detached background slot pre-warmer (if bg-warm + Stage B ran)
$SpawnDeferredWarm = if ($SlotSync -eq "bg-warm" -and -not $SkipSmokeTest) { "1" } else { "0" }
$PrimMigInt = if ($PrimaryMigrated) { "1" } else { "0" }
$GoldRebInt = if ($GoldenRebuilt) { "1" } else { "0" }

$EpilogueScript = @'
set -euo pipefail
common_sh="$1"
prov_sh="$2"
spawn_warm="$3"
head_sha="$4"
prim_mig="$5"
gold_reb="$6"
slot_sync="$7"
sync_ms="$8"
prim_ms="$9"
stga_ms="${10}"
gold_ms="${11}"
stgb_ms="${12}"
tot_ms="${13}"

source "$common_sh"
mkdir -p "$LOCK_DIR"

python3 - "$PROMOTE_HISTORY_FILE" "$head_sha" "$prim_mig" "$gold_reb" "$slot_sync" \
  "$sync_ms" "$prim_ms" "$stga_ms" "$gold_ms" "$stgb_ms" "$tot_ms" <<'PY' >/dev/null 2>&1 || true
import datetime, json, sys
(
    path, head, pmig, greb, ssync,
    sync_ms, prim_ms, stga_ms, gold_ms, stgb_ms, tot_ms
) = sys.argv[1:12]
rec = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "head": head[:8],
    "primary_migrated": bool(int(pmig)),
    "golden_rebuilt": bool(int(greb)),
    "slot_sync": ssync,
    "sync_ms": int(sync_ms),
    "primary_ms": int(prim_ms),
    "stage_a_ms": int(stga_ms),
    "golden_ms": int(gold_ms),
    "stage_b_ms": int(stgb_ms),
    "total_ms": int(tot_ms),
    "rc": 0,
}
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec) + "\n")
PY

if [[ "$spawn_warm" -eq 1 ]]; then
  cur_gen="$(read_golden_gen)"
  if [[ -n "$cur_gen" ]]; then
    needs_warm=0
    for k in 1 2 3 4 5; do
      mf="$LOCK_DIR/slot-$k.meta"
      st="$(read_meta_value "$mf" "db_state")"
      wt="$(read_meta_value "$mf" "last_worktree")"
      sgen="$(read_meta_value "$mf" "golden_gen")"
      if [[ "$sgen" != "$cur_gen" ]] && [[ "$st" == "golden" || "$st" == "restoring" || -z "$st" || "$wt" == "$TRUNK_WORKTREE_ID" || "$wt" == "__GOLDEN__" ]]; then
        needs_warm=1
        break
      fi
    done
    if [[ "$needs_warm" -eq 1 ]]; then
      if command -v setsid >/dev/null 2>&1; then
        setsid bash "$prov_sh" --slots 5 --warm-stale </dev/null >>"$BG_WARM_LOG_FILE" 2>&1 &
      else
        nohup bash "$prov_sh" --slots 5 --warm-stale </dev/null >>"$BG_WARM_LOG_FILE" 2>&1 &
      fi
      echo "[promote-to-main] Background slot pre-warmer started for idle stale slots (log=$BG_WARM_LOG_FILE)."
    fi
  fi
fi
'@
$EpilogueB64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes(($EpilogueScript -replace "`r", "")))
wsl -d $TargetDistro --exec python3 -c 'import base64,subprocess,sys;sys.exit(subprocess.call(sys.argv[2:4]+[base64.b64decode(sys.argv[1]).decode()]+sys.argv[4:]))' `
    $EpilogueB64 bash -c _ $WslCommonPath $WslProvisionPath $SpawnDeferredWarm $HeadSha `
    $PrimMigInt $GoldRebInt $SlotSync "$SyncMs" "$PrimaryMs" "$StageAMs" "$GoldenMs" "$StageBMs" "$TotalMs"

$PrimMode = if ($PrimaryMigrated) { "migrated" } else { "cache-only" }
$GoldMode = if ($SlotSync -eq "none") { "skipped" } elseif ($GoldenRebuilt) { "rebuilt" } else { "cached" }
$Summary = ("sync={0:N1}s, primary={1:N1}s [{2}], stageA={3:N1}s, golden={4:N1}s [{5}], stageB={6:N1}s, slot_sync={7}" -f `
    ($SyncMs / 1000.0), ($PrimaryMs / 1000.0), $PrimMode, ($StageAMs / 1000.0), ($GoldenMs / 1000.0), $GoldMode, ($StageBMs / 1000.0), $SlotSync)

Write-Host ("[promote-to-main] Trunk promotion complete in {0:N1}s ({1})." -f ($TotalMs / 1000.0), $Summary) -ForegroundColor Green
exit 0
