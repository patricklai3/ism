#!/usr/bin/env bash
# Shared constants and helpers for Frappe bench-slot provisioning and leasing.

WSL_DEV_ROOT="/home/real2/projects/.project/frappe_docker/development"
WSL_PRIMARY_BENCH="$WSL_DEV_ROOT/frappe-bench"
WSL_SLOTS_ROOT="$WSL_DEV_ROOT/bench-slots"
CONTAINER_DEV_ROOT="/workspace/development"
CONTAINER_PRIMARY_BENCH="$CONTAINER_DEV_ROOT/frappe-bench"
CONTAINER_SLOTS_ROOT="$CONTAINER_DEV_ROOT/bench-slots"
LOCK_DIR="/tmp/ism-bench-slots"

CONTAINER_FRAPPE="devcontainer-frappe-1"
CONTAINER_DB="devcontainer-mariadb-1"
CONTAINER_REDIS_CACHE="devcontainer-redis-cache-1"
CONTAINER_REDIS_QUEUE="devcontainer-redis-queue-1"
MARIADB_ROOT_PW="123"

GOLDEN_FILE="$WSL_SLOTS_ROOT/golden/main_golden.sql.gz"
GOLDEN_FP_FILE="$WSL_SLOTS_ROOT/golden/golden_schema.fp"
GOLDEN_META_FILE="$WSL_SLOTS_ROOT/golden/golden.meta"
BASE_UPSTREAM_FILE="$WSL_SLOTS_ROOT/golden/base_upstream.sql.gz"
BASE_UPSTREAM_FP_FILE="$WSL_SLOTS_ROOT/golden/base_upstream.fp"
PRIMARY_FP_FILE="$WSL_SLOTS_ROOT/golden/primary_schema.fp"

HISTORY_FILE="$LOCK_DIR/lease-history.jsonl"
PROMOTE_HISTORY_FILE="$LOCK_DIR/promote-history.jsonl"
BG_WARM_PID_FILE="$LOCK_DIR/bg-warm.pid"
BG_WARM_LOG_FILE="$LOCK_DIR/bg-warm.log"
MARIADB_TUNED_FILE="$LOCK_DIR/mariadb-tuned.id"

TRUNK_WORKTREE_ID="${ISM_PRIMARY_WORKTREE_ID:-d:/ism}"

read_meta_value() {
  local meta_file="$1"
  local key="$2"
  if [[ -f "$meta_file" ]]; then
    awk -F= -v k="$key" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$meta_file"
  fi
}

write_meta_file() {
  local meta_file="$1"
  local last_wt="$2"
  local last_branch="$3"
  local last_head="$4"
  local db_state="$5"
  local schema_fp="${6:-}"
  local golden_gen=""
  if [[ $# -ge 7 ]]; then
    golden_gen="$7"
  elif [[ -f "$meta_file" ]]; then
    golden_gen="$(read_meta_value "$meta_file" "golden_gen")"
  fi
  local now_iso now_epoch tmp_file
  now_iso="$(date -Iseconds)"
  now_epoch="$(date +%s)"
  tmp_file="${meta_file}.tmp.$$"

  cat > "$tmp_file" <<META
last_worktree=$last_wt
last_branch=$last_branch
last_head=$last_head
db_state=$db_state
schema_fp=$schema_fp
golden_gen=$golden_gen
updated_at=$now_iso
updated_at_epoch=$now_epoch
META
  mv -f "$tmp_file" "$meta_file"
}

write_golden_meta() {
  local golden_gen="$1"
  local schema_fp="$2"
  local golden_input_fp="$3"
  local upstream_fp="$4"
  local built_head="${5:-unknown}"
  local now_iso now_epoch tmp_file
  now_iso="$(date -Iseconds)"
  now_epoch="$(date +%s)"
  mkdir -p "$(dirname "$GOLDEN_META_FILE")"
  tmp_file="${GOLDEN_META_FILE}.tmp.$$"

  cat > "$tmp_file" <<META
golden_gen=$golden_gen
schema_fp=$schema_fp
golden_input_fp=$golden_input_fp
upstream_fp=$upstream_fp
built_head=$built_head
updated_at=$now_iso
updated_at_epoch=$now_epoch
META
  mv -f "$tmp_file" "$GOLDEN_META_FILE"
}

read_golden_gen() {
  if [[ -f "$GOLDEN_META_FILE" ]]; then
    read_meta_value "$GOLDEN_META_FILE" "golden_gen" | tr -d '[:space:]'
  fi
}

compute_schema_fp() {
  local app_root="$1"
  python3 - "$app_root" <<'PY'
import hashlib
import os
import sys

root = os.path.realpath(sys.argv[1])
if not os.path.isdir(root):
    print("")
    sys.exit(0)

skip_dirs = {".git", ".codegraph", "node_modules", "__pycache__", ".pytest_cache", ".venv", ".idea", ".vscode"}
targets = []

for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
    for fn in sorted(filenames):
        full = os.path.join(dirpath, fn)
        rel = os.path.relpath(full, root).replace("\\", "/")
        if not rel.startswith("ism/"):
            continue
        parts = rel.split("/")
        include = False
        if fn in ("patches.txt", "hooks.py", "setup.py", "install.py"):
            include = True
        elif fn.endswith(".json"):
            include = True
        elif fn.endswith(".py") and ("patches" in parts or "setup_tasks" in parts):
            include = True
        if include:
            targets.append((rel, full))

targets.sort(key=lambda item: item[0])
h = hashlib.sha256()
for rel, full in targets:
    h.update(rel.encode("utf-8"))
    h.update(b"\0")
    try:
        with open(full, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        h.update(b"<missing>")
    h.update(b"\0")

print(h.hexdigest())
PY
}

compute_upstream_fp() {
  local primary_bench="$1"
  local apps_csv="${2:-erpnext,hrms,ism}"
  python3 - "$primary_bench" "$apps_csv" <<'PY'
import hashlib
import json
import os
import subprocess
import sys

primary_bench, apps_csv = sys.argv[1], sys.argv[2]
upstream_apps = ["frappe"]
for raw in apps_csv.split(","):
    a = raw.strip()
    if a and a not in ("frappe", "ism") and a not in upstream_apps:
        upstream_apps.append(a)

h = hashlib.sha256()
h.update(("apps:" + ",".join(upstream_apps)).encode("utf-8"))
h.update(b"\0")

site_cfg_path = os.path.join(primary_bench, "sites", "development.localhost", "site_config.json")
if os.path.isfile(site_cfg_path):
    try:
        enc_key = json.load(open(site_cfg_path, "r", encoding="utf-8")).get("encryption_key", "")
        h.update(b"enc_key_sha:" + hashlib.sha256(enc_key.encode("utf-8")).digest())
        h.update(b"\0")
    except Exception:
        pass

for app in upstream_apps:
    app_dir = os.path.join(primary_bench, "apps", app)
    h.update(f"app:{app}:".encode("utf-8"))
    git_dir = os.path.join(app_dir, ".git")
    if os.path.exists(git_dir):
        try:
            sha = subprocess.check_output(["git", "-C", app_dir, "rev-parse", "HEAD"], text=True).strip()
            h.update(sha.encode("utf-8"))
            dirty = subprocess.check_output(
                ["git", "-C", app_dir, "status", "--porcelain", "--untracked-files=no"], text=True
            ).strip()
            if dirty:
                diff = subprocess.check_output(["git", "-C", app_dir, "diff", "HEAD"])
                h.update(b":dirty:" + hashlib.sha256(diff).digest())
        except Exception:
            h.update(b"git_err")
    elif os.path.isdir(app_dir):
        for rel in ("pyproject.toml", f"{app}/hooks.py", f"{app}/__init__.py"):
            fp = os.path.join(app_dir, rel)
            if os.path.isfile(fp):
                h.update(rel.encode("utf-8") + b":" + open(fp, "rb").read())
    h.update(b"\0")

print(h.hexdigest())
PY
}

compute_golden_input_fp() {
  local app_root="$1"
  local upstream_fp="$2"
  python3 - "$app_root" "$upstream_fp" <<'PY'
import hashlib
import os
import re
import sys

root = os.path.realpath(sys.argv[1])
upstream_fp = sys.argv[2]
if not os.path.isdir(root):
    print("")
    sys.exit(0)

skip_dirs = {".git", ".codegraph", "node_modules", "__pycache__", ".pytest_cache", ".venv", ".idea", ".vscode"}
targets = {}

def add_file(full_path):
    if not os.path.isfile(full_path):
        return
    rel = os.path.relpath(full_path, root).replace("\\", "/")
    targets[rel] = full_path

def add_dotted_module(dotted):
    if not dotted.startswith("ism."):
        return
    parts = dotted.split(".")
    # Try module.py and package/__init__.py along prefixes from longest to 2
    for end in range(len(parts), 1, -1):
        cand_py = os.path.join(root, *parts[:end]) + ".py"
        cand_init = os.path.join(root, *parts[:end], "__init__.py")
        if os.path.isfile(cand_py):
            add_file(cand_py)
            break
        if os.path.isfile(cand_init):
            add_file(cand_init)
            break

add_file(os.path.join(root, "pyproject.toml"))

for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
    for fn in sorted(filenames):
        full = os.path.join(dirpath, fn)
        rel = os.path.relpath(full, root).replace("\\", "/")
        if not rel.startswith("ism/"):
            continue
        parts = rel.split("/")
        include = False
        if fn in ("patches.txt", "hooks.py", "setup.py", "install.py"):
            include = True
        elif fn.endswith(".json"):
            include = True
        elif fn.endswith(".py") and ("patches" in parts or "setup_tasks" in parts):
            include = True
        elif rel in ("ism/testing/catalog.py", "ism/testing/manifest.py", "ism/testing/__init__.py"):
            include = True
        elif rel.startswith("ism/ism/doctype/") and fn.endswith(".py") and not fn.startswith("test_"):
            include = True
        if include:
            targets[rel] = full

# Resolve dotted hooks (doc_events, install/migrate hooks) and local ism.* imports in catalog.py
dotted_re = re.compile(r'["\x27](ism(?:\.[A-Za-z0-9_]+)+)["\x27]')
import_re = re.compile(r'^\s*(?:from|import)\s+(ism(?:\.[A-Za-z0-9_]+)*)', re.MULTILINE)

for probe_rel in ("ism/hooks.py", "ism/setup.py", "ism/testing/catalog.py"):
    probe_full = os.path.join(root, *probe_rel.split("/"))
    if os.path.isfile(probe_full):
        try:
            txt = open(probe_full, "r", encoding="utf-8", errors="replace").read()
            for m in dotted_re.findall(txt):
                add_dotted_module(m)
            for m in import_re.findall(txt):
                add_dotted_module(m)
        except OSError:
            pass

h = hashlib.sha256()
h.update(b"upstream:" + upstream_fp.encode("utf-8") + b"\0")
for rel in sorted(targets.keys()):
    full = targets[rel]
    h.update(rel.encode("utf-8") + b"\0")
    try:
        with open(full, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        h.update(b"<missing>")
    h.update(b"\0")

print(h.hexdigest())
PY
}

now_ms() {
  python3 -c "import time; print(int(time.time() * 1000))"
}

ensure_dev_mariadb_tuning() {
  mkdir -p "$LOCK_DIR"
  local cid_marker=""
  cid_marker="$(docker inspect --format '{{.Id}}|{{.State.StartedAt}}' "$CONTAINER_DB" 2>/dev/null || true)"
  if [[ -n "$cid_marker" && -f "$MARIADB_TUNED_FILE" ]]; then
    local prev_marker
    prev_marker="$(cat "$MARIADB_TUNED_FILE" 2>/dev/null || true)"
    if [[ "$prev_marker" == "$cid_marker" ]]; then
      return 0
    fi
  fi

  if docker exec "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" -e \
    "SET GLOBAL innodb_flush_log_at_trx_commit = 2; SET GLOBAL innodb_flush_sync = 0; SET GLOBAL innodb_io_capacity = 2000; SET GLOBAL innodb_io_capacity_max = 4000; SET GLOBAL innodb_flush_neighbors = 0; SET GLOBAL innodb_stats_on_metadata = 0; SET GLOBAL max_allowed_packet = 67108864;" >/dev/null 2>&1; then
    if [[ -n "$cid_marker" ]]; then
      local tmp_tuned="${MARIADB_TUNED_FILE}.tmp.$$"
      printf '%s\n' "$cid_marker" > "$tmp_tuned"
      mv -f "$tmp_tuned" "$MARIADB_TUNED_FILE"
    fi
  fi
}

dump_db_to_gz() {
  local db_name="$1"
  local out_gz="$2"
  mkdir -p "$(dirname "$out_gz")"
  local tmp_gz="${out_gz}.tmp.$$"
  docker exec "$CONTAINER_DB" mariadb-dump -uroot -p"$MARIADB_ROOT_PW" \
    --quick --extended-insert --skip-add-locks --skip-comments --single-transaction --routines --triggers "$db_name" \
    | gzip -1 > "$tmp_gz"
  if [[ ! -s "$tmp_gz" ]]; then
    rm -f "$tmp_gz"
    echo "[slot-common] MariaDB dump of '$db_name' produced an empty archive." >&2
    return 1
  fi
  mv -f "$tmp_gz" "$out_gz"
}

restore_sql_gz_to_db() {
  local gz_file="$1"
  local db_name="$2"
  local db_pw="$3"

  ensure_dev_mariadb_tuning

  docker exec "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" -e \
    "DROP DATABASE IF EXISTS \`$db_name\`; CREATE DATABASE \`$db_name\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER IF NOT EXISTS '$db_name'@'%' IDENTIFIED BY '$db_pw'; ALTER USER '$db_name'@'%' IDENTIFIED BY '$db_pw'; GRANT ALL PRIVILEGES ON \`$db_name\`.* TO '$db_name'@'%'; FLUSH PRIVILEGES;"

  {
    echo "SET SESSION unique_checks=0; SET SESSION foreign_key_checks=0; SET SESSION autocommit=0;"
    gunzip -c "$gz_file"
    echo "COMMIT;"
  } | docker exec -i "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" "$db_name"
}

write_site_config_json() {
  local site_cfg="$1"
  local db_name="$2"
  local db_pw="$3"
  local enc_key="$4"
  local apps_csv="$5"
  python3 - "$site_cfg" "$db_name" "$db_pw" "$enc_key" "$apps_csv" <<'PY'
import json, os, sys
site_cfg, db_name, db_pw, enc_key, apps_csv = sys.argv[1:6]
os.makedirs(os.path.dirname(site_cfg), exist_ok=True)
cur = json.load(open(site_cfg, "r", encoding="utf-8")) if os.path.isfile(site_cfg) else {}
if enc_key:
    cur["encryption_key"] = enc_key
extra_apps = [a.strip() for a in apps_csv.split(",") if a.strip() and a.strip() != "frappe"]
cur.update({
    "allow_cors": "*",
    "allow_tests": True,
    "db_name": db_name,
    "db_password": db_pw,
    "db_type": "mariadb",
    "db_user": db_name,
    "developer_mode": 1,
    "installed_apps": ["frappe", *extra_apps],
})
tmp = site_cfg + f".tmp.{os.getpid()}"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cur, f, indent=1)
    f.write("\n")
os.replace(tmp, site_cfg)
PY
}

install_clean_ism_site() {
  local bench_wsl_dir="$1"
  local bench_container_dir="$2"
  local site_name="$3"
  local db_name="$4"
  local db_pw="$5"
  local redis_idx="$6"
  local apps_csv="${7:-erpnext,hrms,ism}"
  local force_full_upstream="${8:-0}"
  local save_base_cache="${9:-0}"
  local lock_db_on_restore="${10:-0}"

  ensure_dev_mariadb_tuning

  local site_dir="$bench_wsl_dir/sites/$site_name"
  local site_cfg="$site_dir/site_config.json"
  local common_cfg="$bench_wsl_dir/sites/common_site_config.json"

  mkdir -p \
    "$site_dir/logs" \
    "$site_dir/indexes" \
    "$site_dir/private/files" \
    "$site_dir/public/files" \
    "$site_dir/locks"

  local enc_key=""
  if [[ -f "$site_cfg" ]]; then
    enc_key="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("encryption_key",""))' "$site_cfg" 2>/dev/null || true)"
  fi
  if [[ -z "$enc_key" && -f "$WSL_PRIMARY_BENCH/sites/development.localhost/site_config.json" ]]; then
    enc_key="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("encryption_key",""))' "$WSL_PRIMARY_BENCH/sites/development.localhost/site_config.json" 2>/dev/null || true)"
  fi

  python3 - "$common_cfg" <<'PY'
import json, os, sys
path = sys.argv[1]
cfg = json.load(open(path, "r", encoding="utf-8")) if os.path.isfile(path) else {}
cfg["mariadb_user_host_login_scope"] = "%"
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=1)
    f.write("\n")
PY

  docker exec "$CONTAINER_REDIS_CACHE" redis-cli -n "$redis_idx" FLUSHDB >/dev/null
  docker exec "$CONTAINER_REDIS_QUEUE" redis-cli -n "$redis_idx" FLUSHDB >/dev/null

  local container_path="$bench_container_dir/env/bin:/home/frappe/.local/bin:/home/frappe/.nvm/versions/node/v24.13.0/bin:/home/frappe/.pyenv/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

  local upstream_csv=""
  local has_ism=0
  IFS=',' read -ra _raw_apps <<< "$apps_csv"
  local _up_list=()
  for _a in "${_raw_apps[@]}"; do
    _a="${_a// /}"
    [[ -z "$_a" || "$_a" == "frappe" ]] && continue
    if [[ "$_a" == "ism" ]]; then
      has_ism=1
    else
      _up_list+=("$_a")
    fi
  done
  upstream_csv="$(IFS=','; echo "${_up_list[*]}")"

  local upstream_fp cached_up_fp=""
  upstream_fp="$(compute_upstream_fp "$WSL_PRIMARY_BENCH" "$apps_csv")"
  if [[ -s "$BASE_UPSTREAM_FP_FILE" ]]; then
    cached_up_fp="$(tr -d '[:space:]' < "$BASE_UPSTREAM_FP_FILE")"
  fi

  if [[ "$force_full_upstream" -eq 0 && -s "$BASE_UPSTREAM_FILE" && -n "$upstream_fp" && "$cached_up_fp" == "$upstream_fp" ]]; then
    echo "[clean-install] Restoring cached Tier-0 upstream base (upstream_fp=${upstream_fp:0:8}) onto $site_name ($db_name)..."
    if [[ "$lock_db_on_restore" -eq 1 ]]; then
      (
        exec 202>"$LOCK_DIR/mariadb-restore.lock"
        flock 202
        restore_sql_gz_to_db "$BASE_UPSTREAM_FILE" "$db_name" "$db_pw"
      )
    else
      restore_sql_gz_to_db "$BASE_UPSTREAM_FILE" "$db_name" "$db_pw"
    fi
    write_site_config_json "$site_cfg" "$db_name" "$db_pw" "$enc_key" "$upstream_csv"
  else
    # Cold build: create fresh Frappe site and install upstream apps (erpnext, hrms, etc.).
    docker exec -i -w "$bench_container_dir" "$CONTAINER_FRAPPE" env PATH="$container_path" bash -s -- \
      "$site_name" "$db_name" "$MARIADB_ROOT_PW" <<'EOF'
set -euo pipefail
site="$1"
db_name="$2"
root_pw="$3"

help_txt="$(bench new-site --help 2>&1 || true)"
cmd=(bench new-site "$site" --force --db-name "$db_name" --admin-password admin)
if printf '%s' "$help_txt" | grep -q -- '--mariadb-root-username'; then
  cmd+=(--mariadb-root-username root)
fi
if printf '%s' "$help_txt" | grep -q -- '--mariadb-root-password'; then
  cmd+=(--mariadb-root-password "$root_pw")
elif printf '%s' "$help_txt" | grep -q -- '--db-root-password'; then
  cmd+=(--db-root-password "$root_pw")
fi
if printf '%s' "$help_txt" | grep -q -- '--mariadb-user-host-login-scope'; then
  cmd+=(--mariadb-user-host-login-scope '%')
fi
if printf '%s' "$help_txt" | grep -q -- '--no-mariadb-socket'; then
  cmd+=(--no-mariadb-socket)
fi
echo "[clean-install] Running: ${cmd[*]}"
"${cmd[@]}"
EOF

    docker exec "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" -e \
      "CREATE USER IF NOT EXISTS '$db_name'@'%' IDENTIFIED BY '$db_pw'; ALTER USER '$db_name'@'%' IDENTIFIED BY '$db_pw'; GRANT ALL PRIVILEGES ON \`$db_name\`.* TO '$db_name'@'%'; FLUSH PRIVILEGES;"

    write_site_config_json "$site_cfg" "$db_name" "$db_pw" "$enc_key" ""

    if [[ -n "$upstream_csv" ]]; then
      docker exec -i -w "$bench_container_dir" "$CONTAINER_FRAPPE" env PATH="$container_path" bash -s -- \
        "$site_name" "$upstream_csv" <<'EOF'
set -euo pipefail
site="$1"
upstream_csv="$2"
IFS=',' read -ra apps <<< "$upstream_csv"
for app in "${apps[@]}"; do
  app="${app// /}"
  [[ -z "$app" || "$app" == "frappe" ]] && continue
  echo "[clean-install] Installing upstream app '$app' on $site..."
  bench --site "$site" install-app "$app"
done
bench --site "$site" clear-cache
EOF
    fi
    write_site_config_json "$site_cfg" "$db_name" "$db_pw" "$enc_key" "$upstream_csv"

    if [[ "$save_base_cache" -eq 1 && -n "$upstream_fp" ]]; then
      echo "[clean-install] Caching Tier-0 upstream base snapshot (upstream_fp=${upstream_fp:0:8})..."
      dump_db_to_gz "$db_name" "$BASE_UPSTREAM_FILE"
      local tmp_up_fp="${BASE_UPSTREAM_FP_FILE}.tmp.$$"
      printf '%s\n' "$upstream_fp" > "$tmp_up_fp"
      mv -f "$tmp_up_fp" "$BASE_UPSTREAM_FP_FILE"
    fi
  fi

  if [[ "$has_ism" -eq 1 ]]; then
    docker exec -i -w "$bench_container_dir" "$CONTAINER_FRAPPE" env PATH="$container_path" bash -s -- \
      "$site_name" <<'EOF'
set -euo pipefail
site="$1"
bench --site "$site" clear-cache
echo "[clean-install] Installing app 'ism' on $site..."
bench --site "$site" install-app ism
bench --site "$site" clear-cache
echo "[clean-install] Seeding Layer 1 canonical catalog on $site..."
bench --site "$site" execute ism.testing.catalog.seed_all
EOF
  fi

  write_site_config_json "$site_cfg" "$db_name" "$db_pw" "$enc_key" "$apps_csv"
}
