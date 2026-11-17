# Local Staged-Action Form (`--stage`) — Implementation Plan

Eliminate the post-confirmation agent execution turn in `ism-agent-os` by adding a `--stage` CLI flag backed by a zero-dependency `127.0.0.1` HTTP daemon and interactive HTML/JS form UI. Agents stage write commands on Turn 1 and output a clickable local URL (no browser auto-open, no Antigravity Sidecar wrapper); the user fills any `[MISSING — REQUIRED]` fields, edits optional values, and clicks **Submit** to execute `_COMMAND_HANDLERS` (or chained `_cmd_batch` pipelines) locally (~50% fewer LLM turns on complete writes, ~67% fewer on missing-field writes, ~100–250ms post-submit latency, zero `ism-frappe` changes).

## Execution Strategy (`model-triage`)

| Scope | Execution Mode | Model / Pinning |
| :--- | :--- | :--- |
| Cross-cutting multi-layer feature (CLI parser/chaining, `127.0.0.1` staging daemon/UI, PreToolUse write-gate hook, rules/skills, unit tests) | **Single Coordinator** or **1 `/swarm`** (3 parallel slices: CLI/chaining, daemon/UI, hook/docs/tests) | `Orchestrator (Carbon)` *(or `/swarm`: `gemini-4-hydrogen-medium` authors + `gemini-4-carbon-high` verifier)* |

---

## Locked Architectural Contracts (Freeze Before Coding)

1. **Localhost Daemon Only (No Sidecar):** All runtime components stay inside `ism-agent-os/tools/` using Python stdlib (`http.server.ThreadingHTTPServer`, `pyproject.toml` remains `dependencies = []`). Do not install or generate machine-local `~/.gemini/config/sidecars/` wrappers.
2. **No Browser Auto-Open:** `python -m tools.cli --stage ...` must **never** call `webbrowser.open()` automatically; it returns `{success, staged: true, stage_id, url, ready, missing_required, commands}` and exits `0` immediately so the agent presents the clickable URL and ends its turn.
3. **Non-Blocking Stage Lifecycle:** Staging spawns/reuses a detached background localhost daemon (auto-exiting after 30m idle) instead of blocking `run_command`, preventing post-submit agent wakeups.
4. **Security & Idempotency:** Daemon binds strictly to `127.0.0.1`, validates `Host`, rejects cross-origin `Origin` / `Sec-Fetch-Site: cross-site` on `POST`, enforces per-stage 256-bit `nonce` (`hmac.compare_digest`) + TTL, and locks `pending -> executing -> submitted` under `threading.Lock`.
5. **Dual Write-Gate + Strict Toggle:** `enforce_ism_write_gate.py` keeps supporting `--dry-run` → `ISM Pre-Dispatch Summary` → live CLI alongside `--stage` (preserving `scripts/eval_nl_loop.py`), while adding optional `ISM_STAGE_ONLY=1` strict enforcement.
6. **Follow-Up Recall:** Successful submissions persist `.ism_staged/actions/<stage_id>.json` and `.ism_staged/latest.json` (gitignored) so follow-up agent turns can read created IDs (`RO-...`, `SE-...`, `PO-...`) via `stage-receipt` or `@last`.

---

## Phase 1: Stage Store, Receipt Recall & Batch Chaining

- [x] **1.1 Stage Persistence (`ism-agent-os/tools/stage_store.py` [NEW])**
  - Manage `.ism_staged/` (`actions/<stage_id>.json`, `latest.json`, `server.json`) using atomic temp-file + `os.replace`.
  - Implement `create_stage_record`, `load_stage_record`, `update_stage_record`, `record_receipt`, and `read_latest_receipt`.
- [x] **1.2 Typed Form Schema Builder (`ism-agent-os/tools/cli_validation.py`)**
  - Add `STAGE_FIELD_HINTS` mapping `WRITE_COMMAND_META` fields to UI widget types (`text`, `int`, `float`, `date`, `time`, `select`, `json`, `bool`, `textarea`).
  - Add `"stage-receipt"` to `READ_ONLY_COMMANDS`.
  - Implement `build_stage_form_spec(parsed_items, parser)` preserving existing `validate_write_command_args` and `build_pre_dispatch_summary` contracts.
- [x] **1.3 Multi-Step Chaining & `@last` Recall (`ism-agent-os/tools/cli_commands.py`)**
  - Extend `_cmd_batch` to resolve `$stepN.<field>` and `$prev.<field>` tokens (e.g., `$step0.data.name`) between sequential commands so `quote` → `amend-quote` → `send-approval` runs in one local Submit click.
  - Support `@last` / `@last.<field>` resolution from `.ism_staged/latest.json`.
  - Register `_cmd_stage_receipt` handler for `stage-receipt [--id <ID>]`.

## Phase 2: Localhost Daemon & Interactive Form UI

- [x] **2.1 Localhost Staging Server (`ism-agent-os/tools/stage_server.py` [NEW])**
  - Implement `ThreadingHTTPServer(("127.0.0.1", port), StageRequestHandler)` with `Host`, `Origin` / `Sec-Fetch-Site`, `nonce`, TTL, and single-use `threading.Lock` guards.
  - Expose `GET /health`, `GET /?id=<id>&token=<nonce>`, `GET /api/stage/<id>`, and `POST /api/stage/<id>/submit`.
  - Validate submitted payloads with `validate_write_command_args` + `validate_temporal_args`, execute single or chained batch commands via `IsmAgentClient(IsmConfig.from_env())`, write receipt to `.ism_staged/latest.json`, and pass through `409`/`417` scheduling conflict alternatives.
  - Implement `ensure_server_running(staged_dir)` to ping `.ism_staged/server.json` `/health` or spawn a detached background daemon (`CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` on Windows, `start_new_session=True` on POSIX) with 1800s idle auto-exit.
- [x] **2.2 Self-Contained Form UI (`ism-agent-os/tools/stage_ui.html` [NEW])**
  - Render multi-step form cards with target endpoint badges, autofocus on first `[MISSING — REQUIRED]` field, client-side required checks, and collapsible optional fields.
  - Show created IDs (`RO-...`, `SE-...`, `PO-...`), totals, and clickable Frappe `urls` (`desk`, `print`, `approval`) after Submit.
  - Render scheduling conflict `alternatives` / `available_slots` as one-click **Select Slot & Retry** pills.

## Phase 3: CLI Integration, Write-Gate Hook & Git Hygiene

- [x] **3.1 CLI Parser & Entrypoint (`ism-agent-os/tools/cli_parsers.py`, `cli.py`)**
  - Add `--stage` flag on root parser and subparsers (`default=argparse.SUPPRESS`) plus `stage-receipt [--id <ID>]` subcommand.
  - Handle `--stage` in `cli.main()`: parse single or `batch` commands (allowing missing required fields through to UI; validating `$stepN` bounds), create stage record, ensure daemon is running, print JSON `{success: True, staged: True, stage_id, url, ready, missing_required, commands}` with **no** `webbrowser.open()`, and exit `0`.
- [x] **3.2 Write-Gate Hook & Gitignore (`.agents/scripts/enforce_ism_write_gate.py`, `.gitignore`)**
  - Treat `--stage` identically to `--dry-run` in `_parse_write_commands` (allowed without `ISM Pre-Dispatch Summary`).
  - Add optional `ISM_STAGE_ONLY=1` env guard to block non-staged live write commands when strict mode is enabled, keeping default `--dry-run -> confirm -> live` intact.
  - Add `.ism_staged/` to `.gitignore`.

## Phase 4: Rules, Skills & Verification

- [x] **4.1 Rules & Skills (`ism-agent-os/rules/AGENTS.md`, `skills/*/SKILL.md`)**
  - Document `--stage` and `stage-receipt` in `rules/AGENTS.md` (staying under 24,000 bytes) and the 4 write-capable `SKILL.md` files (`ism-service-advisor`, `ism-shop-dispatcher`, `ism-floor-technician`, `ism-parts-specialist`).
- [x] **4.2 Automated Unit Tests (`ism-agent-os/tests/`)**
  - Update `test_client_and_cli.py`: test `--stage` single & `batch` without `IsmAgentClient` calls or browser launch, `$stepN`/`$prev` batch chaining, `stage-receipt`, and `@last` resolution.
  - Update `test_write_gate_hook.py`: test `--stage` allow without summary, legacy `--dry-run -> confirm -> live`, and `ISM_STAGE_ONLY=1` strict denial.
  - Add `test_stage_server.py`: test `403` Origin/nonce guards, `400` missing-field validation keeping `pending`, `200` execution writing `latest.json`, `409` double-submit lock, chained batch execution, and conflict retry payload.
  - Run full suite: `python -m unittest discover -s ism-agent-os/tests -t ism-agent-os -v`.
- [x] **4.3 Live Slot Smoke Verification**
  - Stage `quote` missing `--odometer-in` via `tools.cli --json --stage`, verify CLI exits immediately with no browser auto-open, open printed URL manually, enter odometer and submit, verify `RO-...` in Frappe and follow-up `stage-receipt` recall.
