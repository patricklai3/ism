---
name: ism-floor-technician
description: >-
  Executes shop floor Repair Order transitions including starting bay work, placing jobs
  on hold for parts, and recording completion with odometer out. Use when a technician
  starts, pauses, or finishes work on a vehicle.
---

# Floor Technician Operations

**NEVER run `--help`, `-h`, or `Get-ChildItem skills/`**. The table below lists exact CLI flags—call `python -m tools.cli --json <cmd> ...` (alias: `ism-agent-os`) directly on turn 1 without redundant pre-checks (`status`/`slots`/`tech-overview`/`get-doc`/`estimate`). Target 1-2 CLI turns for simple requests and 3 max for multi-step workflows.

## Direct Intent-to-Command Routing

| User Intent | Direct Command |
| :--- | :--- |
| Start, hold, park, or complete an RO | `python -m tools.cli --json job-status --repair-order <RO> --action <start\|hold\|park\|complete> [--odometer-out <KM>] [--notes <TEXT>]` |
| Check off (or reopen) completed service rows | `python -m tools.cli --json toggle-service --repair-order <RO> (--service-row <ROW1> [<ROW2> ...] \| --all) [--reopen]` |
| Read check-in walkaround inspection | `python -m tools.cli --json get-inspection --repair-order <RO>` |
| Record inspection findings, odometer, concern | `python -m tools.cli --json record-inspection --repair-order <RO> --items-json <@file\|JSON> [--odometer-in <KM>] [--color <COLOR>] [--license-plate <PLATE>] [--concern <TEXT>] [--template <NAME>] [--auto-apply]` |
| List or apply inspection template | `python -m tools.cli --json inspection-templates [--template <NAME>]` / `python -m tools.cli --json apply-inspection --repair-order <RO> [--template <NAME>]` |
| Add mid-job supplement for approval | `python -m tools.cli --json add-supplement --repair-order <RO> --title <TITLE> --hours <HRS> [--parts-json <@file\|JSON>] [--notes <TEXT>]` |
| Duplicate symmetrical LH/RH service row | `python -m tools.cli --json duplicate-side --repair-order <RO> --service-row <ROW>` |
| Check stock for parts hold | `python -m tools.cli --json check-stock --repair-order <RO>` |
| Log floor discrepancy / escalation | `python -m tools.cli --json create-action --title <TITLE> [--ref-doctype "Repair Order" --ref-name <RO>] [--details <TEXT>]` |
| Read Shop Board action card(s) + RO transcript | `python -m tools.cli --json action-cards [--action-item <ACT-1> [<ACT-2> ...]] [--repair-order <RO>] [--status Open]` |
| Post direct floor message on active RO thread | `python -m tools.cli --json board-post --repair-order <RO> --text <TEXT> [--intent <TAG>] [--run-ai]` |
| Clear / resolve handled action card (direct) | `python -m tools.cli --json resolve-action --action-item <ACT-ID> [--notes <TEXT>]` |
| Checkout, collect payment, release vehicle | `python -m tools.cli --json checkout --repair-order <RO> --confirm [--payment-method <METHOD>] [--paid-amount <AMT>] [--odometer-out <KM>]` |
| Multi-step floor actions in one process | `python -m tools.cli --json batch --cmd "job-status --repair-order <RO> --action hold" --cmd "check-stock --repair-order <RO>"` (or `--file @cmds.json`; alias: `ism-agent-os batch`) |

## Routing Rules

1. **Zero `--help` or redundant pre-checks**: Use the exact flags in the table above on turn 1. Run `job-status --action start` directly when asked to start an RO; do not run `--help`, `status`, `estimate`, or `get-doc` first (and never run `estimate` before `job-status --action complete` or `toggle-service`). The server enforces the authorization lock—if rejected because `approval_status` is not `Approved` or `Partially Approved`, stop immediately, notify the Service Advisor, and run `send-approval` only if instructed.
2. **Batch multi-step floor updates**: Use `python -m tools.cli --json batch --cmd "..." --cmd "..."` (or `--file @cmds.json`) instead of PowerShell stdin pipes for chained actions (e.g., `job-status --action hold` + `check-stock`, or `toggle-service --repair-order <RO> --all` + `job-status --repair-order <RO> --action complete`). Pass `--all` on `toggle-service` to mark all active/approved rows complete without looking up row IDs first, or pass multiple rows to `toggle-service --service-row <R1> <R2>` in one call.
3. **Human confirmation for checkout**: Never run `checkout --confirm` without explicit human confirmation.
4. **Exception-only `/board` vs `/tech`**: Technicians work assigned jobs in `/tech`; use `board-post` only for floor exceptions or direct technician communication (routine state transitions are no longer echoed onto `/board`).

## Required Fields, `--stage` & Pre-Dispatch Callback

- **`record-inspection` check-in requirements**: Vehicle **color** (`--color` / `--vehicle-color`) and **license plate** (`--license-plate` / `--plate`) are required on check-in if not already recorded on the `Repair Vehicle`.
- **Human-facing vehicle references**: Refer to vehicles by **year, color, model, and license plate** (not VIN-only). Under tight space, drop VIN first, then year and plate (keep color + model).
- **`job-status --action complete` required fields**: `--repair-order`, `--action complete`, and `--odometer-out` (`>= odometer_in`).
- **`checkout` required fields**: `--repair-order`, `--payment-method` (no silent default), `--odometer-out` (`>= odometer_in`), and `--confirm`.
- **Preferred Turn-1 Staged Form (`--stage`)**: Stage floor writes (`job-status`, `toggle-service`, `record-inspection`, `add-supplement`, `duplicate-side`, `checkout`) via `python -m tools.cli --json --stage <subcmd ...>` (or `--stage batch` with PowerShell-safe `@stepN.<field>` / `@prev.<field>`; `stepN.<field>` and `$stepN.<field>` also work, but NEVER put unescaped `$` like `$step0` or `$100` inside PowerShell `"..."` strings), present the returned `127.0.0.1` URL for the technician/advisor to fill missing fields (like `--odometer-out` or `--payment-method` + `--confirm`) and submit locally, and recall submitted results with `python -m tools.cli --json stage-receipt` or `@last`.
- **Legacy Callback (`--dry-run`)**: When not using `--stage` (and `ISM_STAGE_ONLY` is unset), run `--dry-run` first, output `### ISM Pre-Dispatch Summary`, and wait for explicit user confirmation before executing any floor write or checkout.

## Verification

- Verify `success: true` and `data.status` reflects the target state (`Authorized - In Progress`, `Waiting on Parts`, `Work Completed`, or `Closed / Released`).
