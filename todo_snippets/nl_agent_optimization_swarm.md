# Iterative NL-Agent Optimization Swarm v3 — Procedures & Checklist

Run a closed-loop simulation and optimization swarm across ISM's 3-layer stratified testing pyramid (**24 CLI Contract Scenarios** $\rightarrow$ **12-Category × 10-Seed Monte Carlo Sweep** $\rightarrow$ **40 Cold Blinded `Gemini 4 Hydrogen (Low)` NL Scenarios** across `eval_standard` and `swarm_crunch_day`), using isolated bench slots, `--stage` local confirmation forms, and Tesla-offset parts pricing.

---

## Execution Strategy (`model-triage` — Gemini 4 Only)

| Role | Scope & Ownership | Model / Pinning |
| :--- | :--- | :--- |
| **`coordinator`** | Owns loop state, Wave 1/Wave 2 gating, round caps, and escalation; never edits app code | **Gemini 4 Carbon (High)** *(or **Gemini 4 Argon (High)**)* |
| **`monitor`** | Standard `swarm_monitor` stall watcher | `gemini-4-hydrogen-low` |
| **`eval-runner`** | Slot bootstrap (`slot-1` / `slot-2`), `stage_server` daemon (`IsDaemon=true`), sharded blinded evals, `submit-stage`, `score` | **`gemini-4-hydrogen-xhigh`** throughout orchestration; spawns cold blinded helpers via sharded `swarm{action:"add", model:"gemini-4-hydrogen-low", workspace:"inherit", tasks:[...]}` |
| **`trace-reviewer`** | Read-only analyst grading traces, DB checks, `--stage` schemas, and ranking skill vs CLI vs controller friction | `gemini-4-hydrogen-xhigh` |
| **`cli-skills-author`** | `ism-agent-os/skills/ism-*`, `rules/AGENTS.md` (`< 24,000` bytes), `tools/cli*.py`, `tools/stage_*.py`, `ism-agent-os/tests/` | `gemini-4-hydrogen-high` *(or `gemini-4-hydrogen-xhigh`)* |
| **`controller-author`** | `ism-frappe/ism/api/*.py`, `ism-frappe/ism/scheduling/*.py`, `ism-frappe/ism/tests/` | `gemini-4-hydrogen-high` *(or `gemini-4-hydrogen-xhigh`)* |
| **`verifier`** | Cross-family read/test gate on `slot-3` (`ism-frappe` suite, `ism-agent-os` `unittest`, `eval_scenarios.py`, `sim_runner.py`, legacy `--dry-run` canary) | `gemini-4-argon-high` *(or `gemini-4-carbon-high`)* |

> **Hard Guardrails:** Never use `Gemini 3.8 Flash`, `Claude`, `GPT-OSS`, `Model="flash"`, or leaf `Orchestrator`. Always use `swarm{action:"model"}` for escalation—never `kill` + `add` to change a waiting worker's model.

---

## Locked Architectural Invariants (Swarm v3)

1. **Zero `development.localhost:8000` Contact:**
   - Never run `sync-to-wsl.ps1`, `promote-to-main.ps1`, or direct tests against `development.localhost` from worktrees or swarm workers.
   - Run all simulation seeding, HTTP serving, and Frappe test suites in isolated bench slots via `ism-frappe/scripts/test-in-slot.ps1` (`slot-1`: `eval_standard` on `:8001`; `slot-2`: `swarm_crunch_day` + `verifier` suites on `:8002`; `slot-3`/`slot-4` for parallel `workspace: "share"` authors if needed).
2. **Safe `.env` Isolation & Cleanup:**
   - Always bootstrap via `python scripts/eval_nl_loop.py bootstrap-slot --slot <N> --profile <PROFILE> --activate-dotenv --manifest-out scripts/eval_results/<profile>_manifest.json`, which backs up `ism-agent-os/.env` to `.ism_staged/env.slot_backup`.
   - Always finish rounds with `teardown-slot --slot <N> --restore-dotenv`.
3. **Antigravity Windows Stage Daemon (`--stage` Primary):**
   - Start `python -m tools.stage_server` once with `IsDaemon=true` in `ism-agent-os/` after `bootstrap-slot` so Windows Job Objects do not kill the `127.0.0.1` form daemon between `--stage` CLI invocations.
   - Cold blinded agents follow `rules/AGENTS.md` (preferring `--stage` on Turn 1, outputting the `127.0.0.1` URL); `eval-runner` simulates operator form inspection & submission via `python scripts/eval_nl_loop.py submit-stage --url "<URL>" [--expect-missing ...] [--set ...]`.
4. **Cold Sub-Agent Execution via Sharded `swarm{action:"add"}`:**
   - Swarm workers lack `invoke_subagent` / `send_message` (and `run_workflow` inside a worker cannot find `"self"`); `eval-runner` stays on `gemini-4-hydrogen-high` throughout orchestration and spawns cold blinded test helpers via sharded `swarm{action:"add", model:"gemini-4-hydrogen-low", workspace:"inherit", tasks:[...]}` in two waves (`Wave 1` reads -> `Wave 2` writes/traps), saving traces to `scripts/eval_results/r<N>_traces/res_<ID>.json` and killing each wave's helper group once collected.
   - For the 2-scenario legacy `--dry-run` verifier canary (`S19`, `S25`), spawned `gemini-4-hydrogen-low` helpers emit `### ISM Pre-Dispatch Summary` in assistant text / Turn 1 result and wait for Turn 2 confirmation before executing live writes.
5. **Repo Hygiene Guards:**
   - Keep `ism-agent-os/rules/AGENTS.md` strictly `< 24,000` bytes and pass `test_skills_validation.py` (zero stale legacy protocol substring hits anywhere in `ism-agent-os/`).

---

## 3-Layer Stratified Testing Pyramid

1. **Layer 1 — Deterministic CLI Contract Gate (`eval_scenarios.py`, 0 LLM tokens, ~5s):**
   - `24` scenarios (`E01`–`E24`) covering all 5 personas (`dispatcher`, `advisor`, `parts`, `technician`, `executive`), `--tags`, `--min-time`, `price-part` 4-regime pricing, `$step0` / `$prev` batch chaining, `409`/`417`/leave/offline traps, and `--dry-run` missing-field gates.
2. **Layer 2 — Multi-Seed Monte Carlo Resilience (`sim_runner.py`, 0 LLM tokens, ~45s):**
   - `12` probe categories across `seeds 1..10` (`~120` probes): `baseline`, `leave_collision`, `conflict`, `stockout`, `unscheduled`, `over_tolerance`, `lunch_break_boundary`, `offline_bay_guard`, `stockout_alternates`, `partially_approved_deferred`, `dry_run_write_gate`, and `pricing_floor_contract`.
3. **Layer 3 — Blinded `Gemini 4 Hydrogen (Low)` NL Suite (`eval_nl_loop.py`, 40 Scenarios, `2.5%` KPI resolution):**
   - **`eval_standard` (`S01`–`S30` on `slot-1`, `--relative-dates`):**
     - **Wave 1 (Reads, `S01`–`S11`)**: Floor status (`S01`), Elena leave (`S02`), Alignment Rack (`S03`), afternoon `--min-time` (`S04`), unscheduled queue (`S05`), VIN + history `batch` (`S06`), plate `LEV-301` + deferred hazard (`S07`), 3-read `batch` (`S08`), rotor alternates + inspection templates (`S09`), `price-part` baseline + small-ticket (`S10`), `price-part` cost-floor + fallback with zero Tesla OEM leakage (`S11`).
     - **Wave 2 (Writes & Traps, `S12`–`S30`)**: `intake-slots` (`S12`), `quote` + `amend-quote` + `send-approval` `$step0` chain (`S13`), `quote` + `@last` `estimate` (`S14`), symmetrical LH/RH `duplicate-side` (`S15`), household multi-owner `add-deferred` (`S16`), `apply-inspection` (`S17`), flat-rate `quote` + `$100` `deposit` (`S18`), direct `book` 24h (`S19`), `409` conflict without pre-`slots` (`S20`), `417` ambiguous `'Bay'` recovery (`S21`), Elena leave + `12:00` break trap (`S22`), `reschedule` + `appointment-status` (`S23`), walk-in `create-ro-from-entry` missing `--odometer-in` gate (`S24`), `quote` missing `--odometer-in` gate (`S25`), unapproved `job-status --action start` lock (`S26`), standalone `procure` omitting `--paid-amount` (`S27`), `purchase-invoice --update-stock` (`S28`), wrong-supplier open PO stop (`S29`), and 2-step electronic `purchase-invoice` + `receive-po` (`S30`).
   - **`swarm_crunch_day` (`C01`–`C10` on `slot-2`, ~85.3% load, `--relative-dates`):**
     - **Wave 1 (Reads, `C01`–`C03`)**: Executive floor & action-item briefing (`C01`), commercial fleet 5-EV WIP + deferred rollup (`C02`), packed-floor post-`15:45` slot search (`C03`).
     - **Wave 2 (Floor, FIFO & Exec Writes, `C04`–`C10`)**: FIFO loser `check-stock` + RO-linked `procure` (`C04`), mid-job over-tolerance `add-supplement` (`C05`), supplement `send-approval` + `resolve-action` (`C06`), parts hold + `check-stock` + `create-action` (`C07`), `toggle-service` + `job-status --action complete` missing `--odometer-out` gate (`C08`), `checkout` missing `--payment-method` gate + `Sales Invoice` release (`C09`), and unapproved supplement checkout block (`C10`).

---

## Primary KPIs & Termination Guardrails

| Metric | Swarm v3 Target | Scope |
| :--- | :---: | :--- |
| `cli_contract_pass_rate` | **`100%`** (`24/24`) | `eval_scenarios.py` |
| `monte_carlo_probe_pass_rate` | **`100%`** (`10/10` seeds) | `sim_runner.py --seed-range 1..10 --strict` |
| `db_verification_rate` | **`100%`** (`40/40`) | Live Frappe DB + output assertions |
| `first_try_success` | **`>= 95.0%`** (`>= 38/40`, aim `40/40`) | `0` `--help`, `0` self-corrections, `0` redundant calls, `0` hook denials |
| `avg_domain_cli_calls` | **`<= 1.30`** | `live_cli_calls + staged_calls` across all 40 scenarios |
| `anti_hallucination_gate_rate` | **`100%`** (`6/6`) | `S24`, `S25`, `C08`, `C09` missing fields + `S26`, `C10` auth locks |
| `conflict_and_trap_recovery_rate` | **`100%`** (`5/5`) | `S20`, `S21`, `S22`, `S29`, `C10` |

- **Round Cap & Inner-Round Slicing:** Max `3` optimization rounds after Round 0. Inner fix rounds (Rounds 1–2) re-run only failed `--ids` + affected `--skill` + 5 golden canaries (`S06,S13,S20,S25,C04`) via `eval_nl_loop.py scenarios --ids ...` before running the full 40-scenario final gate.
- **Diminishing Returns / Abort:** Stop if `first_try_success` and `avg_domain_cli_calls` both improve `< 5%` after Round 1, or abort & revert after `2` consecutive broken rounds.

---

## Phase 1: Harness & Procedure Upgrade (Pre-Swarm Foundation)

- [x] **1.1 Monte Carlo & Slot Helpers (`ism-agent-os/scripts/sim_runner.py`)**
  - Add `default_offline_crunch_manifest`, `PROFILE_EXECUTE_FNS`, `configure_slot_dotenv` / `restore_slot_dotenv` (`.ism_staged/env.slot_backup`), and expand `build_monte_carlo_probes` to `12` probe categories across default `seeds 1..10`.
- [x] **1.2 Deterministic 24-Scenario CLI Suite (`ism-agent-os/scripts/eval_scenarios.py`)**
  - Upgrade from 10 to `24` contract scenarios (`E01`–`E24`) covering all 5 shop personas, Tesla-offset pricing, `$step0` chaining, conflict/leave/offline traps, and `--dry-run` missing-field gates.
- [x] **1.3 40-Scenario Blinded NL Loop & `--stage` Submitter (`ism-agent-os/scripts/eval_nl_loop.py`)**
  - Add `S01`–`S30` + `C01`–`C10` with `--profile`, `--tier`, `--skill`, `--wave`, `--ids`, `--relative-dates`, `--stage`-aware `_analyze_commands`, `bootstrap-slot`, `submit-stage`, and `teardown-slot`.
- [x] **1.4 Unit Tests & Procedure Doc (`ism-agent-os/tests/test_sim_runner.py`, `todo_snippets/nl_agent_optimization_swarm.md`)**
  - Verify `40` NL scenarios, `24` CLI contracts, `12`-category Monte Carlo probes, `--stage` scoring, in-process `submit-stage`, `.env` backup/restore, and zero forbidden legacy tokens.

---

## Phase 2: Swarm v3 Execution Checklist (Next `/swarm` Session)

- [x] **2.1 Baseline Gate (`verifier` on `slot-3`, parallel with `eval-runner` setup)**
  - Run `powershell -ExecutionPolicy Bypass -File ism-frappe\scripts\test-in-slot.ps1 -Slot 3 -Module ism.tests.test_simulation_framework,ism.tests.test_agent_facade` (or full app suite).
  - Run `python -m unittest discover -s ism-agent-os/tests -t ism-agent-os -v`.
  - Run `python ism-agent-os/scripts/sim_runner.py --seed-range 1..10 --slot 3 --json --strict`.
- [x] **2.2 Round 0 Baseline (`eval-runner` -> `trace-reviewer`)**
  - Bootstrap `slot-1` (`eval_standard`, `--activate-dotenv`) + start `python -m tools.stage_server` (`IsDaemon=true`), then run `eval_scenarios.py --strict` (`24/24`).
  - Keep `eval-runner` on `gemini-4-hydrogen-high`; spawn cold blinded `gemini-4-hydrogen-low` helpers via sharded `swarm{action:"add", model:"gemini-4-hydrogen-low", workspace:"inherit", tasks:[...]}` for `S01`–`S30` in two waves (`Wave 1`, then `Wave 2` + `submit-stage` + Turn 2s + `score --round 0 --profile eval_standard`), saving traces to `scripts/eval_results/r0_traces/res_<ID>.json` and killing each wave's helpers once collected.
  - Bootstrap `slot-2` (`swarm_crunch_day`, `--activate-dotenv`), run `C01`–`C10` in two waves (`score --round 0 --profile swarm_crunch_day`), and verify 2-scenario legacy `--dry-run` canary (`S19`, `S25`).
- [x] **2.3 Closed-Loop Optimization (Rounds 1–3, capped at 3)**
  - `trace-reviewer` (`gemini-4-hydrogen-high`) ranks friction items from `round_<N>.json`.
  - `cli-skills-author` / `controller-author` (`gemini-4-hydrogen-medium`) land minimal surgical fixes.
  - `verifier` (`gemini-4-carbon-high`) runs `ism-frappe` + `ism-agent-os` + `sim_runner.py` gates; `eval-runner` runs targeted `--ids` / `--skill` slice (or full `40`-scenario final gate).
- [x] **2.4 Clean Teardown & Report**
  - Run `teardown-slot --slot 1 --restore-dotenv` and `teardown-slot --slot 2`, confirm `git status` clean of stray temp artifacts, kill non-monitor workers, and post Round 0 $\rightarrow$ Final KPI delta table.

---

## Phase 2 Execution Summary (Swarm v3 — Completed)

| Metric | Round 0 Baseline | Final (Round 3 + `S16` Fix) | Target |
| :--- | :---: | :---: | :---: |
| `cli_contract_pass_rate` | `22/24` (`91.7%`) | **`24/24` (`100.0%`)** | `100%` |
| `monte_carlo_probe_pass_rate` | `0/10` seeds (`138/167` / `82.63%` probes) | **`10/10` seeds (`170/170` / `100.0%` probes)** | `100%` |
| `db_verification_rate` | `3/3` scored (`incomplete` harness) | **`40/40` (`100.0%`)** | `100%` |
| `first_try_success` | — | **`40/40` (`100.0%`)** | `>= 95.0%` |
| `avg_domain_cli_calls` | — | **`1.10`** (`0.45` live, `0.65` staged) | `<= 1.30` |
| `anti_hallucination_gate_rate` | — | **`6/6` (`100.0%`)** | `100%` |
| `conflict_and_trap_recovery_rate` | — | **`5/5` (`100.0%`)** | `100%` |
| Legacy `--dry-run` canary (`S19,S25`) | — | **`2/2` (`100.0%`)** | `100%` |
