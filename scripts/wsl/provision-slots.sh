#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=slot-common.sh
source "$SCRIPT_DIR/slot-common.sh"

SLOT_COUNT=5
REFRESH_GOLDEN=0
FORCE_GOLDEN=0
REBUILD_BASE=0
RECREATE=0
SLOT_SYNC="auto"
WARM_STALE=0
STATUS_ONLY=0
HISTORY_LIMIT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slots)
      SLOT_COUNT="${2:-5}"
      shift 2
      ;;
    --refresh-golden)
      REFRESH_GOLDEN=1
      shift
      ;;
    --force-golden)
      REFRESH_GOLDEN=1
      FORCE_GOLDEN=1
      shift
      ;;
    --rebuild-base)
      REFRESH_GOLDEN=1
      FORCE_GOLDEN=1
      REBUILD_BASE=1
      shift
      ;;
    --recreate)
      RECREATE=1
      shift
      ;;
    --slot-sync)
      SLOT_SYNC="${2:-auto}"
      shift 2
      ;;
    --warm-stale)
      WARM_STALE=1
      shift
      ;;
    --status)
      STATUS_ONLY=1
      shift
      ;;
    --history)
      if [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]]; then
        HISTORY_LIMIT="$2"
        shift 2
      else
        HISTORY_LIMIT=20
        shift
      fi
      ;;
    *)
      echo "[provision-slots] Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if ! [[ "$SLOT_COUNT" =~ ^[1-8]$ ]]; then
  echo "[provision-slots] --slots must be between 1 and 8 (got '$SLOT_COUNT')." >&2
  exit 2
fi

case "$SLOT_SYNC" in
  auto|bg-warm|lazy|eager|none) ;;
  *)
    echo "[provision-slots] --slot-sync must be auto|bg-warm|lazy|eager|none (got '$SLOT_SYNC')." >&2
    exit 2
    ;;
esac

mkdir -p "$LOCK_DIR"

bg_warmer_status() {
  if [[ -f "$BG_WARM_PID_FILE" ]]; then
    local wpid
    wpid="$(cat "$BG_WARM_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$wpid" ]] && kill -0 "$wpid" 2>/dev/null; then
      printf 'running(pid=%s)' "$wpid"
      return 0
    fi
  fi
  printf 'idle'
}

print_status() {
  local cur_gen cur_fp cur_infp base_state warmer_state
  cur_gen="$(read_golden_gen)"
  cur_fp="$(read_meta_value "$GOLDEN_META_FILE" "schema_fp")"
  [[ -z "$cur_fp" && -s "$GOLDEN_FP_FILE" ]] && cur_fp="$(tr -d '[:space:]' < "$GOLDEN_FP_FILE")"
  cur_infp="$(read_meta_value "$GOLDEN_META_FILE" "golden_input_fp")"
  base_state="missing"
  [[ -s "$BASE_UPSTREAM_FILE" ]] && base_state="cached($(du -h "$BASE_UPSTREAM_FILE" | cut -f1))"
  warmer_state="$(bg_warmer_status)"

  printf "[golden] gen=%s schema_fp=%s input_fp=%s base_upstream=%s bg_warmer=%s\n" \
    "${cur_gen:--}" "${cur_fp:0:8}" "${cur_infp:0:8}" "$base_state" "$warmer_state"

  printf "%-7s %-11s %-7s %-10s %-8s %-10s %-40s %-24s %-10s %-26s\n" \
    "SLOT" "PROVISIONED" "LOCK" "DB_STATE" "GOLDEN" "SCHEMA_FP" "LAST_WORKTREE" "BRANCH" "HEAD" "SERVE"
  for k in $(seq 1 "$SLOT_COUNT"); do
    local slot_host_dir="$WSL_SLOTS_ROOT/slot-$k"
    local prov="missing"
    if [[ -d "$slot_host_dir/env" && -f "$slot_host_dir/sites/wt${k}.localhost/site_config.json" ]]; then
      prov="ready"
    fi

    local lock_file="$LOCK_DIR/slot-$k.lock"
    local lock_state="free"
    if ! ( exec 9>"$lock_file"; flock -n 9 ); then
      lock_state="busy"
    fi

    local meta_file="$LOCK_DIR/slot-$k.meta"
    local last_wt last_branch last_head db_state schema_fp slot_gen golden_col
    last_wt="$(read_meta_value "$meta_file" "last_worktree")"
    last_branch="$(read_meta_value "$meta_file" "last_branch")"
    last_head="$(read_meta_value "$meta_file" "last_head")"
    db_state="$(read_meta_value "$meta_file" "db_state")"
    schema_fp="$(read_meta_value "$meta_file" "schema_fp")"
    slot_gen="$(read_meta_value "$meta_file" "golden_gen")"

    golden_col="-"
    if [[ -n "$cur_gen" && -n "$db_state" ]]; then
      if [[ "$slot_gen" == "$cur_gen" ]]; then
        golden_col="fresh"
      elif [[ ( "$db_state" == "ready" || "$db_state" == "dirty" ) && -n "$last_wt" && "$last_wt" != "__GOLDEN__" && "$last_wt" != "$TRUNK_WORKTREE_ID" ]]; then
        golden_col="sticky"
      else
        golden_col="stale"
      fi
    fi

    if [[ "$lock_state" == "busy" && -f "$LOCK_DIR/slot-$k.holder" ]]; then
      local holder_line holder_wt holder_branch
      holder_line="$(cat "$LOCK_DIR/slot-$k.holder" 2>/dev/null || true)"
      IFS='|' read -r holder_wt holder_branch _ _ <<< "$holder_line"
      [[ -n "$holder_wt" ]] && last_wt="$holder_wt"
      [[ -n "$holder_branch" ]] && last_branch="$holder_branch"
    fi

    [[ -z "$last_wt" ]] && last_wt="-"
    [[ -z "$last_branch" ]] && last_branch="-"
    [[ -z "$last_head" ]] && last_head="-"
    [[ -z "$db_state" ]] && db_state="-"
    if [[ -z "$schema_fp" ]]; then
      schema_fp="-"
    elif [[ "$schema_fp" != "migrating" && ${#schema_fp} -gt 8 ]]; then
      schema_fp="${schema_fp:0:8}"
    fi
    if [[ ${#last_head} -gt 8 ]]; then
      last_head="${last_head:0:8}"
    fi

    local serve_info="-"
    local pid_file="$LOCK_DIR/slot-$k-serve.pid"
    if [[ -f "$pid_file" ]]; then
      local spid
      spid="$(cat "$pid_file" 2>/dev/null || true)"
      if [[ -n "$spid" ]] && kill -0 "$spid" 2>/dev/null; then
        serve_info="http://localhost:800${k} (pid $spid)"
      fi
    fi

    printf "%-7s %-11s %-7s %-10s %-8s %-10s %-40s %-24s %-10s %-26s\n" \
      "slot-$k" "$prov" "$lock_state" "$db_state" "$golden_col" "$schema_fp" "$last_wt" "$last_branch" "$last_head" "$serve_info"
  done
}

print_history() {
  local limit="$1"
  if [[ ! -s "$HISTORY_FILE" ]]; then
    echo "[provision-slots] No slot lease history recorded yet at $HISTORY_FILE."
  else
    python3 - "$HISTORY_FILE" "$limit" <<'PY'
import json
import sys
from collections import Counter

history_path, limit_str = sys.argv[1], sys.argv[2]
limit = max(1, int(limit_str))

rows = []
with open(history_path, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

if not rows:
    print("[provision-slots] History file is empty or unreadable.")
    sys.exit(0)

total = len(rows)
tiers = Counter(r.get("tier", "?") for r in rows)
restores = sum(1 for r in rows if r.get("need_reset"))
migrates = sum(1 for r in rows if r.get("ran_migrate"))
failures = sum(1 for r in rows if r.get("rc", 0) != 0)

def avg(key):
    vals = [r.get(key, 0) for r in rows if isinstance(r.get(key), (int, float))]
    return (sum(vals) / len(vals)) if vals else 0.0

print(
    f"[slot-history] total={total} restores={restores} ({100*restores/total:.1f}%) "
    f"migrates={migrates} ({100*migrates/total:.1f}%) failures={failures} "
    f"tiers={dict(sorted(tiers.items()))}"
)
print(
    f"[slot-history] avg_ms: wait={avg('wait_ms'):.0f} restore={avg('restore_ms'):.0f} "
    f"sync={avg('sync_ms'):.0f} exec={avg('exec_ms'):.0f}"
)
print(
    f"{'TIMESTAMP':<20} {'SLOT':<6} {'TIER':<9} {'RST':<4} {'MIG':<4} "
    f"{'WAIT':>6} {'REST':>6} {'EXEC':>6} {'RC':>3} {'MODE':<8} {'BRANCH':<26} {'WORKTREE'}"
)
for r in rows[-limit:]:
    ts = str(r.get("ts", ""))[:19]
    slot = f"slot-{r.get('slot', '?')}"
    tier = str(r.get("tier", "?"))[:9]
    rst = "yes" if r.get("need_reset") else "no"
    mig = "yes" if r.get("ran_migrate") else "no"
    wait_s = f"{r.get('wait_ms', 0)/1000:.1f}s"
    rest_s = f"{r.get('restore_ms', 0)/1000:.1f}s"
    exec_s = f"{r.get('exec_ms', 0)/1000:.1f}s"
    rc = r.get("rc", "?")
    mode = str(r.get("mode", "?"))[:8]
    branch = str(r.get("branch", "?"))[:26]
    wt = str(r.get("worktree", "?")).rstrip("/").split("/")[-1]
    print(
        f"{ts:<20} {slot:<6} {tier:<9} {rst:<4} {mig:<4} "
        f"{wait_s:>6} {rest_s:>6} {exec_s:>6} {str(rc):>3} {mode:<8} {branch:<26} {wt}"
    )
PY
  fi

  if [[ -s "$PROMOTE_HISTORY_FILE" ]]; then
    echo ""
    python3 - "$PROMOTE_HISTORY_FILE" "$limit" <<'PY'
import json
import sys

path, limit_str = sys.argv[1], sys.argv[2]
limit = max(1, int(limit_str))
rows = []
with open(path, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

if rows:
    print(f"[promote-history] recent={min(limit, len(rows))}/{len(rows)}")
    print(
        f"{'TIMESTAMP':<20} {'HEAD':<9} {'MIG':<4} {'GLD':<6} {'SLOT_SYNC':<9} "
        f"{'SYNC':>6} {'PRIM':>6} {'GLDN':>6} {'STGB':>6} {'TOTAL':>7} {'RC':>3}"
    )
    for r in rows[-limit:]:
        ts = str(r.get("ts", ""))[:19]
        hd = str(r.get("head", "?"))[:8]
        mig = "yes" if r.get("primary_migrated") else "no"
        gld = "reblt" if r.get("golden_rebuilt") else "cache"
        ss = str(r.get("slot_sync", "?"))[:9]
        s_ms = f"{r.get('sync_ms', 0)/1000:.1f}s"
        p_ms = f"{r.get('primary_ms', 0)/1000:.1f}s"
        g_ms = f"{r.get('golden_ms', 0)/1000:.1f}s"
        b_ms = f"{r.get('stage_b_ms', 0)/1000:.1f}s"
        t_ms = f"{r.get('total_ms', 0)/1000:.1f}s"
        rc = r.get("rc", "?")
        print(
            f"{ts:<20} {hd:<9} {mig:<4} {gld:<6} {ss:<9} "
            f"{s_ms:>6} {p_ms:>6} {g_ms:>6} {b_ms:>6} {t_ms:>7} {str(rc):>3}"
        )
PY
  fi
}

if [[ "$STATUS_ONLY" -eq 1 || "$HISTORY_LIMIT" -gt 0 ]]; then
  if [[ "$STATUS_ONLY" -eq 1 ]]; then
    print_status
  fi
  if [[ "$HISTORY_LIMIT" -gt 0 ]]; then
    [[ "$STATUS_ONLY" -eq 1 ]] && echo ""
    print_history "$HISTORY_LIMIT"
  fi
  exit 0
fi

if [[ ! -d "$WSL_PRIMARY_BENCH/env" || ! -f "$WSL_PRIMARY_BENCH/sites/development.localhost/site_config.json" ]]; then
  echo "[provision-slots] Primary bench missing at $WSL_PRIMARY_BENCH" >&2
  exit 1
fi

mkdir -p "$WSL_SLOTS_ROOT/golden"

read_primary_apps_csv() {
  python3 - "$WSL_PRIMARY_BENCH/sites/development.localhost/site_config.json" <<'PY'
import json, os, sys
p = sys.argv[1]
apps = json.load(open(p, "r", encoding="utf-8")).get("installed_apps", []) if os.path.isfile(p) else []
apps = [a for a in apps if a and a != "frappe"]
print(",".join(apps) if apps else "erpnext,hrms,ism")
PY
}

ensure_slot_asset_symlinks_host() {
  local slot_host_dir="$1"
  local assets_dir="$slot_host_dir/sites/assets"
  [[ -d "$assets_dir" ]] || return 0
  for item in assets.json assets-rtl.json css js locale; do
    if [[ -e "$WSL_PRIMARY_BENCH/sites/assets/$item" ]]; then
      if [[ -e "$assets_dir/$item" && ! -L "$assets_dir/$item" ]]; then
        rm -rf "$assets_dir/$item"
      fi
      ln -sfn "../../../../frappe-bench/sites/assets/$item" "$assets_dir/$item"
    fi
  done
}

provision_bench_fs_and_venv() {
  local bench_dir_name="$1"
  local site_name="$2"
  local db_name="$3"
  local db_pw="$4"
  local redis_idx="$5"
  local refresh_ism="${6:-0}"
  local host_slot_dir="$WSL_SLOTS_ROOT/$bench_dir_name"

  mkdir -p "$host_slot_dir/apps"
  if [[ ! -f "$host_slot_dir/apps/ism/pyproject.toml" || "$refresh_ism" -eq 1 ]]; then
    for stale_name in .git .codegraph ism-agent-os scripts; do
      if [[ ! -e "$WSL_PRIMARY_BENCH/apps/ism/$stale_name" && -e "$host_slot_dir/apps/ism/$stale_name" ]]; then
        rm -rf "$host_slot_dir/apps/ism/$stale_name"
      fi
    done
    rsync -a --delete \
      --exclude='.git' \
      --exclude='.codegraph' \
      --exclude='node_modules' \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='.pytest_cache' \
      --exclude='.venv' \
      --exclude='.idea' \
      --exclude='.vscode' \
      "$WSL_PRIMARY_BENCH/apps/ism/" "$host_slot_dir/apps/ism/"
  fi

  docker exec -i "$CONTAINER_FRAPPE" bash -s -- \
    "$bench_dir_name" "$site_name" "$db_name" "$db_pw" "$redis_idx" \
    "$CONTAINER_PRIMARY_BENCH" "$CONTAINER_SLOTS_ROOT" <<'EOF'
set -euo pipefail
BENCH_DIR_NAME="$1"
SITE_NAME="$2"
SLOT_DB="$3"
SLOT_PW="$4"
REDIS_IDX="$5"
PRIMARY_BENCH="$6"
SLOTS_ROOT="$7"
SLOT_DIR="$SLOTS_ROOT/$BENCH_DIR_NAME"

mkdir -p \
  "$SLOT_DIR/apps" \
  "$SLOT_DIR/logs" \
  "$SLOT_DIR/config/pids" \
  "$SLOT_DIR/sites/assets" \
  "$SLOT_DIR/sites/$SITE_NAME/logs" \
  "$SLOT_DIR/sites/$SITE_NAME/indexes" \
  "$SLOT_DIR/sites/$SITE_NAME/private/files" \
  "$SLOT_DIR/sites/$SITE_NAME/public/files" \
  "$SLOT_DIR/sites/$SITE_NAME/locks"

for app in frappe erpnext hrms induct_shop; do
  if [[ -d "$PRIMARY_BENCH/apps/$app" ]]; then
    ln -sfn "$PRIMARY_BENCH/apps/$app" "$SLOT_DIR/apps/$app"
  fi
done

if [[ ! -d "$SLOT_DIR/env" ]]; then
  echo "[provision-slots] Hardlink-cloning env for $BENCH_DIR_NAME..."
  cp -al "$PRIMARY_BENCH/env" "$SLOT_DIR/env"

  # Break hardlinks and rewrite shebangs in slot env/bin/* so wrapper scripts stay isolated.
  for f in "$SLOT_DIR/env/bin/"*; do
    [[ -f "$f" && ! -L "$f" ]] || continue
    first_line="$(head -n 1 "$f" 2>/dev/null || true)"
    if [[ "$first_line" == "#!${PRIMARY_BENCH}/env/bin/"* ]]; then
      python3 - "$f" "${PRIMARY_BENCH}/env/bin/" "${SLOT_DIR}/env/bin/" <<'PY'
import os, sys
path, old, new = sys.argv[1], sys.argv[2].encode("utf-8"), sys.argv[3].encode("utf-8")
data = open(path, "rb").read()
nl = data.find(b"\n")
head = data[:nl] if nl != -1 else data
rest = data[nl:] if nl != -1 else b""
if head.startswith(b"#!" + old):
    new_data = b"#!" + new + head[2 + len(old):] + rest
    tmp = path + ".tmp"
    with open(tmp, "wb") as out:
        out.write(new_data)
    st = os.stat(path)
    os.chmod(tmp, st.st_mode)
    os.replace(tmp, path)
PY
    fi
  done

  SITE_PKGS="$SLOT_DIR/env/lib/python3.14/site-packages"
  rm -rf "$SITE_PKGS/ism.pth" "$SITE_PKGS"/ism-*.dist-info
  /home/frappe/.local/bin/uv pip install \
    --python "$SLOT_DIR/env/bin/python" \
    --offline --no-deps --link-mode=copy \
    -e "$SLOT_DIR/apps/ism"
fi

# Verify Python resolves ism strictly inside this bench directory
"$SLOT_DIR/env/bin/python" -c "import ism, os; p=os.path.realpath(ism.__file__); expected='$SLOT_DIR/apps/ism/'; assert p.startswith(expected), f'{p} does not start with {expected}'"

cp -f "$PRIMARY_BENCH/sites/apps.txt" "$SLOT_DIR/sites/apps.txt"
cp -f "$PRIMARY_BENCH/sites/apps.json" "$SLOT_DIR/sites/apps.json"

for app in frappe erpnext hrms induct_shop; do
  if [[ -d "$PRIMARY_BENCH/apps/$app/$app/public" ]]; then
    ln -sfn "$PRIMARY_BENCH/apps/$app/$app/public" "$SLOT_DIR/sites/assets/$app"
  fi
done
ln -sfn "$SLOT_DIR/apps/ism/ism/public" "$SLOT_DIR/sites/assets/ism"

for item in assets.json assets-rtl.json css js locale; do
  if [[ -e "$PRIMARY_BENCH/sites/assets/$item" ]]; then
    if [[ -e "$SLOT_DIR/sites/assets/$item" && ! -L "$SLOT_DIR/sites/assets/$item" ]]; then
      rm -rf "$SLOT_DIR/sites/assets/$item"
    fi
    ln -sfn "../../../../frappe-bench/sites/assets/$item" "$SLOT_DIR/sites/assets/$item"
  fi
done

python3 - "$PRIMARY_BENCH" "$SLOT_DIR" "$REDIS_IDX" "$SITE_NAME" "$SLOT_DB" "$SLOT_PW" <<'PY'
import json, os, sys
primary_bench, slot_dir, idx_str, site_name, slot_db, slot_pw = sys.argv[1:7]
idx = int(idx_str)

with open(os.path.join(primary_bench, "sites", "common_site_config.json"), "r", encoding="utf-8") as f:
    common = json.load(f)

common.update({
    "background_workers": 1,
    "default_site": site_name,
    "file_watcher_port": 6790 + idx,
    "gunicorn_workers": 2,
    "live_reload": False,
    "mariadb_user_host_login_scope": "%",
    "redis_cache": f"redis://redis-cache:6379/{idx}?health_check_interval=30",
    "redis_queue": f"redis://redis-queue:6379/{idx}?health_check_interval=30",
    "redis_socketio": f"redis://redis-queue:6379/{idx}",
    "serve_default_site": True,
    "socketio_port": 9000 + idx,
    "webserver_port": 8000 + idx,
})

with open(os.path.join(slot_dir, "sites", "common_site_config.json"), "w", encoding="utf-8") as f:
    json.dump(common, f, indent=1)
    f.write("\n")

with open(os.path.join(primary_bench, "sites", "development.localhost", "site_config.json"), "r", encoding="utf-8") as f:
    primary_site = json.load(f)

site_cfg = {
    "allow_cors": "*",
    "allow_tests": True,
    "db_name": slot_db,
    "db_password": slot_pw,
    "db_type": "mariadb",
    "db_user": slot_db,
    "developer_mode": 1,
}
if "encryption_key" in primary_site:
    site_cfg["encryption_key"] = primary_site["encryption_key"]
if "installed_apps" in primary_site:
    site_cfg["installed_apps"] = primary_site["installed_apps"]

with open(os.path.join(slot_dir, "sites", site_name, "site_config.json"), "w", encoding="utf-8") as f:
    json.dump(site_cfg, f, indent=1)
    f.write("\n")
PY
EOF
}

provision_slot_fs_and_venv() {
  local k="$1"
  local refresh_ism="${2:-0}"
  provision_bench_fs_and_venv "slot-${k}" "wt${k}.localhost" "_ism_slot_${k}" "ism_slot_${k}_pw" "$k" "$refresh_ism"
}

seed_slot_from_golden() {
  local k="$1"
  local slot_db="_ism_slot_${k}"
  local slot_pw="ism_slot_${k}_pw"
  local site_name="wt${k}.localhost"
  local meta_file="$LOCK_DIR/slot-$k.meta"

  echo "[provision-slots] Seeding MariaDB '$slot_db' & Redis /$k for slot-$k..."
  write_meta_file "$meta_file" "__GOLDEN__" "main" "golden" "restoring" "" ""

  local snap_gen="" snap_fp=""
  exec 203>"$LOCK_DIR/golden-build.lock"
  flock -s 203
  snap_gen="$(read_golden_gen)"
  snap_fp="$(read_meta_value "$GOLDEN_META_FILE" "schema_fp")"
  [[ -z "$snap_fp" && -s "$GOLDEN_FP_FILE" ]] && snap_fp="$(tr -d '[:space:]' < "$GOLDEN_FP_FILE")"
  (
    exec 202>"$LOCK_DIR/mariadb-restore.lock"
    flock 202
    restore_sql_gz_to_db "$GOLDEN_FILE" "$slot_db" "$slot_pw"
  )
  exec 203>&-

  docker exec "$CONTAINER_REDIS_CACHE" redis-cli -n "$k" FLUSHDB >/dev/null
  docker exec "$CONTAINER_REDIS_QUEUE" redis-cli -n "$k" FLUSHDB >/dev/null

  local slot_fp
  slot_fp="$(compute_schema_fp "$WSL_SLOTS_ROOT/slot-$k/apps/ism")"
  if [[ -n "$snap_fp" && "$slot_fp" != "$snap_fp" ]]; then
    echo "[provision-slots] slot-$k schema differs from golden (${snap_fp:0:8} -> ${slot_fp:0:8}); running bench migrate..."
    docker exec -w "$CONTAINER_SLOTS_ROOT/slot-$k" "$CONTAINER_FRAPPE" bash -c \
      "export PATH=\"$CONTAINER_SLOTS_ROOT/slot-$k/env/bin:/home/frappe/.local/bin:/home/frappe/.nvm/versions/node/v24.13.0/bin:/home/frappe/.pyenv/shims:\$PATH\"; bench --site '$site_name' migrate"
  fi

  write_meta_file "$meta_file" "__GOLDEN__" "main" "golden" "golden" "$slot_fp" "$snap_gen"
}

warm_stale_slots() {
  exec 204>"$LOCK_DIR/bg-warm.lock"
  if ! flock -n 204; then
    echo "[bg-warm] Background warmer already active; coalescing."
    return 0
  fi

  printf '%s\n' "$$" > "$BG_WARM_PID_FILE"
  trap 'rm -f "$BG_WARM_PID_FILE"' EXIT

  local pass
  for pass in 1 2 3; do
    local target_gen
    target_gen="$(read_golden_gen)"
    if [[ -z "$target_gen" || ! -s "$GOLDEN_FILE" ]]; then
      break
    fi

    for k in $(seq 1 "$SLOT_COUNT"); do
      (
        exec 200>"$LOCK_DIR/slot-$k.lock"
        if ! flock -n 200; then
          echo "[bg-warm] slot-$k is busy; skipping."
          exit 0
        fi

        local slot_host_dir="$WSL_SLOTS_ROOT/slot-$k"
        local meta_file="$LOCK_DIR/slot-$k.meta"
        local cur_st cur_wt cur_gen
        cur_st="$(read_meta_value "$meta_file" "db_state")"
        cur_wt="$(read_meta_value "$meta_file" "last_worktree")"
        cur_gen="$(read_meta_value "$meta_file" "golden_gen")"

        if [[ ! -d "$slot_host_dir/env" || ! -f "$slot_host_dir/sites/wt${k}.localhost/site_config.json" ]]; then
          provision_slot_fs_and_venv "$k" 1
          seed_slot_from_golden "$k"
          echo "[bg-warm] Provisioned & warmed slot-$k (gen=$target_gen)."
          exit 0
        fi

        ensure_slot_asset_symlinks_host "$slot_host_dir"

        if [[ ( "$cur_st" == "golden" || "$cur_st" == "ready" ) && "$cur_gen" == "$target_gen" ]]; then
          exit 0
        fi

        if [[ ( "$cur_st" == "ready" || "$cur_st" == "dirty" ) && -n "$cur_wt" && "$cur_wt" != "__GOLDEN__" && "$cur_wt" != "$TRUNK_WORKTREE_ID" ]]; then
          echo "[bg-warm] Preserving sticky feature worktree on slot-$k ($cur_wt)."
          exit 0
        fi

        provision_slot_fs_and_venv "$k" 1
        seed_slot_from_golden "$k"
        echo "[bg-warm] Warmed slot-$k to golden gen=$target_gen."
      )
    done

    local latest_gen
    latest_gen="$(read_golden_gen)"
    if [[ "$latest_gen" == "$target_gen" ]]; then
      break
    fi
  done
}

if [[ "$WARM_STALE" -eq 1 ]]; then
  warm_stale_slots
  exit 0
fi

spawn_bg_warmer() {
  local cur_gen="$1"
  local needs_warm=0
  for k in $(seq 1 "$SLOT_COUNT"); do
    local slot_host_dir="$WSL_SLOTS_ROOT/slot-$k"
    local mf="$LOCK_DIR/slot-$k.meta"
    local st wt sgen
    st="$(read_meta_value "$mf" "db_state")"
    wt="$(read_meta_value "$mf" "last_worktree")"
    sgen="$(read_meta_value "$mf" "golden_gen")"
    if [[ ! -d "$slot_host_dir/env" ]]; then
      needs_warm=1
      break
    fi
    if [[ "$sgen" != "$cur_gen" ]]; then
      if [[ "$st" == "golden" || "$st" == "restoring" || -z "$st" || "$wt" == "$TRUNK_WORKTREE_ID" || "$wt" == "__GOLDEN__" ]]; then
        needs_warm=1
        break
      fi
    fi
  done

  if [[ "$needs_warm" -eq 1 ]]; then
    if command -v setsid >/dev/null 2>&1; then
      setsid bash "$SCRIPT_DIR/provision-slots.sh" --slots "$SLOT_COUNT" --warm-stale \
        </dev/null >>"$BG_WARM_LOG_FILE" 2>&1 &
    else
      nohup bash "$SCRIPT_DIR/provision-slots.sh" --slots "$SLOT_COUNT" --warm-stale \
        </dev/null >>"$BG_WARM_LOG_FILE" 2>&1 &
    fi
    local bg_pid=$!
    echo "[provision-slots] Spawned detached background slot pre-warmer (pid=$bg_pid, log=$BG_WARM_LOG_FILE)."
  fi
}

GOLDEN_REBUILT=0

build_or_reuse_golden_snapshot() {
  exec 203>"$LOCK_DIR/golden-build.lock"
  flock 203

  local apps_csv upstream_fp schema_fp golden_input_fp prev_input_fp cur_gen
  apps_csv="$(read_primary_apps_csv)"
  upstream_fp="$(compute_upstream_fp "$WSL_PRIMARY_BENCH" "$apps_csv")"
  schema_fp="$(compute_schema_fp "$WSL_PRIMARY_BENCH/apps/ism")"
  golden_input_fp="$(compute_golden_input_fp "$WSL_PRIMARY_BENCH/apps/ism" "$upstream_fp")"
  prev_input_fp="$(read_meta_value "$GOLDEN_META_FILE" "golden_input_fp")"
  cur_gen="$(read_golden_gen)"

  local force_cold_base=0
  if [[ "$REBUILD_BASE" -eq 1 || "$RECREATE" -eq 1 ]]; then
    force_cold_base=1
  fi

  local must_build=0
  if [[ ! -s "$GOLDEN_FILE" || "$RECREATE" -eq 1 || "$FORCE_GOLDEN" -eq 1 || "$REBUILD_BASE" -eq 1 ]]; then
    must_build=1
  elif [[ "$REFRESH_GOLDEN" -eq 1 ]]; then
    if [[ -z "$prev_input_fp" || "$prev_input_fp" != "$golden_input_fp" || -z "$cur_gen" ]]; then
      must_build=1
    fi
  fi

  if [[ "$must_build" -eq 0 ]]; then
    if [[ ! -s "$GOLDEN_FP_FILE" ]]; then
      local tmp_fp="${GOLDEN_FP_FILE}.tmp.$$"
      printf '%s\n' "$schema_fp" > "$tmp_fp"
      mv -f "$tmp_fp" "$GOLDEN_FP_FILE"
    fi
    if [[ -z "$cur_gen" ]]; then
      local gz_sha
      gz_sha="$(sha256sum "$GOLDEN_FILE" | cut -c1-8)"
      cur_gen="legacy-${gz_sha}"
      write_golden_meta "$cur_gen" "$schema_fp" "$golden_input_fp" "$upstream_fp" "existing"
    fi
    if [[ "$REFRESH_GOLDEN" -eq 1 ]]; then
      echo "[provision-slots] Golden DB unchanged (input_fp=${golden_input_fp:0:8}, schema_fp=${schema_fp:0:8}, gen=${cur_gen}); skipping rebuild."
    fi
    GOLDEN_FP="$schema_fp"
    GOLDEN_GEN="$cur_gen"
    GOLDEN_REBUILT=0
    exec 203>&-
    return 0
  fi

  echo "[provision-slots] Building clean Golden DB snapshot (input_fp=${golden_input_fp:0:8}, upstream_fp=${upstream_fp:0:8}, force_base=${force_cold_base})..."

  local golden_wsl="$WSL_SLOTS_ROOT/golden-site"
  local golden_ctr="$CONTAINER_SLOTS_ROOT/golden-site"
  local golden_site="golden.localhost"
  local golden_db="_ism_golden_build"
  local golden_pw="ism_golden_pw"

  provision_bench_fs_and_venv "golden-site" "$golden_site" "$golden_db" "$golden_pw" "9" "1"

  install_clean_ism_site \
    "$golden_wsl" "$golden_ctr" "$golden_site" "$golden_db" "$golden_pw" "9" \
    "$apps_csv" "$force_cold_base" "1" "1"

  docker exec -w "$golden_ctr/sites" "$CONTAINER_FRAPPE" "$golden_ctr/env/bin/python" - <<'PY'
import frappe
frappe.init(site="golden.localhost", sites_path=".")
frappe.connect()
try:
    assert frappe.db.exists("Company", "Lightspeed EV"), "Lightspeed EV missing from clean golden site"
    assert frappe.db.count("Repair Order") == 0, "Expected 0 Repair Orders in clean golden site"
    bad_bays = frappe.get_all("Service Bay", filters={"name": ["like", "Test %"]}, pluck="name")
    assert not bad_bays, f"Unexpected legacy Service Bays: {bad_bays}"
finally:
    frappe.destroy()
PY

  dump_db_to_gz "$golden_db" "$GOLDEN_FILE"

  local tmp_fp="${GOLDEN_FP_FILE}.tmp.$$"
  printf '%s\n' "$schema_fp" > "$tmp_fp"
  mv -f "$tmp_fp" "$GOLDEN_FP_FILE"

  local gz_sha new_gen
  gz_sha="$(sha256sum "$GOLDEN_FILE" | cut -c1-8)"
  new_gen="$(date +%s)-${gz_sha}"
  write_golden_meta "$new_gen" "$schema_fp" "$golden_input_fp" "$upstream_fp" "rebuilt"

  GOLDEN_FP="$schema_fp"
  GOLDEN_GEN="$new_gen"
  GOLDEN_REBUILT=1
  echo "[provision-slots] Clean Golden DB snapshot updated ($(du -h "$GOLDEN_FILE" | cut -f1), schema_fp=${GOLDEN_FP:0:8}, gen=${GOLDEN_GEN})."
  exec 203>&-
}

EFFECTIVE_SYNC="$SLOT_SYNC"
if [[ "$EFFECTIVE_SYNC" == "auto" ]]; then
  if [[ "$RECREATE" -eq 1 ]]; then
    EFFECTIVE_SYNC="eager"
  elif [[ "$REFRESH_GOLDEN" -eq 1 ]]; then
    EFFECTIVE_SYNC="bg-warm"
  else
    EFFECTIVE_SYNC="lazy"
  fi
fi

if [[ "$EFFECTIVE_SYNC" == "none" && "$RECREATE" -eq 0 && -s "$GOLDEN_FILE" ]]; then
  echo "[provision-slots] --slot-sync none: skipping golden refresh and slot updates."
  exit 0
fi

build_or_reuse_golden_snapshot

for k in $(seq 1 "$SLOT_COUNT"); do
  slot_host_dir="$WSL_SLOTS_ROOT/slot-$k"
  meta_file="$LOCK_DIR/slot-$k.meta"
  needs_create=0
  if [[ "$RECREATE" -eq 1 || ! -d "$slot_host_dir/env" || ! -d "$slot_host_dir/sites/wt${k}.localhost/logs" || ! -f "$slot_host_dir/sites/wt${k}.localhost/site_config.json" ]]; then
    needs_create=1
  fi

  if [[ "$needs_create" -eq 1 || "$EFFECTIVE_SYNC" == "eager" ]]; then
    (
      exec 200>"$LOCK_DIR/slot-$k.lock"
      flock 200

      if [[ "$RECREATE" -eq 1 && -d "$slot_host_dir" ]]; then
        echo "[provision-slots] Recreating slot-$k..."
        rm -rf "$slot_host_dir"
      fi

      provision_slot_fs_and_venv "$k" "1"
      seed_slot_from_golden "$k"
      echo "[provision-slots] slot-$k ready (wt${k}.localhost, port 800${k}, redis /$k, gen=$GOLDEN_GEN)."
    )
  else
    # Non-blocking lightweight maintenance on idle slots (asset symlink upgrade & legacy metadata backfill).
    (
      exec 200>"$LOCK_DIR/slot-$k.lock"
      if flock -n 200; then
        ensure_slot_asset_symlinks_host "$slot_host_dir"
        cur_db_state="$(read_meta_value "$meta_file" "db_state")"
        cur_fp="$(read_meta_value "$meta_file" "schema_fp")"
        cur_gen="$(read_meta_value "$meta_file" "golden_gen")"
        cur_epoch="$(read_meta_value "$meta_file" "updated_at_epoch")"
        if [[ "$GOLDEN_REBUILT" -eq 0 && -z "$cur_gen" && "$cur_db_state" == "golden" && "$cur_fp" == "$GOLDEN_FP" ]]; then
          write_meta_file "$meta_file" "__GOLDEN__" "main" "golden" "golden" "$GOLDEN_FP" "$GOLDEN_GEN"
        elif [[ -z "$cur_fp" || -z "$cur_epoch" ]] && [[ "$cur_db_state" == "golden" || "$cur_db_state" == "dirty" || "$cur_db_state" == "ready" ]]; then
          cur_wt="$(read_meta_value "$meta_file" "last_worktree")"
          cur_br="$(read_meta_value "$meta_file" "last_branch")"
          cur_hd="$(read_meta_value "$meta_file" "last_head")"
          [[ "$cur_db_state" == "dirty" ]] && cur_db_state="ready"
          if [[ -z "$cur_fp" ]]; then
            if [[ "$cur_db_state" == "golden" ]]; then
              cur_fp="$GOLDEN_FP"
            else
              cur_fp="$(compute_schema_fp "$slot_host_dir/apps/ism")"
            fi
          fi
          write_meta_file "$meta_file" "${cur_wt:-__GOLDEN__}" "${cur_br:-main}" "${cur_hd:-golden}" "$cur_db_state" "$cur_fp" "$cur_gen"
        fi
      fi
    )
  fi
done

if [[ "$EFFECTIVE_SYNC" == "bg-warm" ]]; then
  spawn_bg_warmer "$GOLDEN_GEN"
fi

echo "[provision-slots] Completed (slots=$SLOT_COUNT, sync=$EFFECTIVE_SYNC, golden_rebuilt=$GOLDEN_REBUILT, gen=$GOLDEN_GEN)."
