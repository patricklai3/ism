---
name: ism-shop-dispatcher
description: >-
  Manages dual-resource bay and technician scheduling, floor capacity checks,
  unscheduled service detection, and ambient wallboard updates in Frappe ISM. Use when
  checking shop availability, booking appointments, or resolving scheduling bottlenecks.
---

# Shop Dispatcher Operations

**NEVER run `--help`, `-h`, or `Get-ChildItem skills/`**. The table below lists exact CLI flags—call `python -m tools.cli --json <cmd> ...` (alias: `ism-agent-os`) directly on turn 1 without redundant pre-checks (`status`/`slots`/`tech-overview` for today, or `estimate`). Target 1-2 CLI turns for simple requests and 3 max for multi-step workflows.

## Direct Intent-to-Command Routing

| User Intent | Direct Command |
| :--- | :--- |
| Live floor status, active bays, on-duty techs | `python -m tools.cli --json status [--date <YYYY-MM-DD>]` |
| Per-technician utilization or leave check | `python -m tools.cli --json tech-overview [--date <YYYY-MM-DD>]` |
| Unscheduled operations waiting on an RO | `python -m tools.cli --json unscheduled --repair-order <RO>` |
| Probabilistic P80/P50 job duration estimate | `python -m tools.cli --json estimate --repair-order <RO>` (or `--operations-json <JSON\|@file>`) |
| Find open appointment slots | `python -m tools.cli --json slots [--repair-order <RO>] [--duration <MIN>] [--date <YYYY-MM-DD>] [--days N] [--tags <TAG>] [--bay <BAY>] [--technician <TECH>] [--min-time <HH:MM[:SS]>] [--include-past] [--include-resources]` |
| Create Draft Quote + return filtered slots in one call | `python -m tools.cli --json intake-slots --customer <NAME> --vehicle <VEH> [--color <COLOR>] [--license-plate <PLATE>] [--service <TITLE_OR_PKG>] [--services-json <@file\|JSON>] [--date <YYYY-MM-DD>] [--days N] [--tags <TAG>] [--bay <BAY>] [--technician <TECH>]` |
| Book appointment (date/time known) | `python -m tools.cli --json book --date <YYYY-MM-DD> --time <HH:MM[:SS]> [--repair-order <RO>] [--bay <BAY>] [--tech <TECH>] [--duration <MIN>] [--entry-type <Diagnostic\|Repair>] [--provisional-customer <NAME>] [--provisional-vehicle <INFO>] [--notes <TEXT>]` |
| Reschedule existing Schedule Entry | `python -m tools.cli --json reschedule --schedule-entry <SE> [--date <YYYY-MM-DD>] [--time <HH:MM[:SS]>] [--bay <BAY>] [--tech <TECH>] [--duration <MIN>]` |
| Update appointment status (`Confirmed`, `No Show`, `Cancelled`, etc.) | `python -m tools.cli --json appointment-status --schedule-entry <SE> --status <STATUS> [--notes <TEXT>]` |
| Convert walk-in / diagnostic entry to RO | `python -m tools.cli --json create-ro-from-entry --schedule-entry <SE> --odometer-in <KM> [--color <COLOR>] [--license-plate <PLATE>]` |
| Multi-step dispatch query/action in one process | `python -m tools.cli --json batch --cmd "status --date <YYYY-MM-DD>" --cmd "tech-overview --date <YYYY-MM-DD>"` (or `--file @cmds.json`; alias: `ism-agent-os batch`) |

## Shop Reference Values & Concrete Examples

- **Equipment tags (`--tags`)**: `Lift`, `Alignment Rack`, `HV Diagnostic` (pass exact tag names directly; do not run `slots --include-resources` just to discover tags).
- **Strict ISO dates & 24h times**: Always convert relative dates (`today`, `tomorrow`, `friday`, `next monday`, `+2d`) to `YYYY-MM-DD` and 12h/named times (`9am`, `2:30pm`, `noon`) to 24-hour `HH:MM:SS` before calling the CLI. If today's date is unknown, run `status` (omitting `--date`) first to read the server `date`. For vague windows (`morning`, `afternoon`), query `slots --date <YYYY-MM-DD> --min-time <08:00:00|13:00:00>` (filtering morning slots `< 12:00`) instead of guessing a fixed booking time.
- **Human / partial identifiers**: `--bay` accepts partial names (e.g., `--bay "Main Lift"`), and `--tech` / `--technician` accept first names or full names (e.g., `--tech Devon`, `--technician Marcus`).
- **Walk-in / provisional booking (omit `--repair-order`)**:
  `python -m tools.cli --json book --date <YYYY-MM-DD> --time 10:00:00 --bay "Bay 1" --entry-type Diagnostic --provisional-customer "<NAME>" --provisional-vehicle "<INFO>" --duration 60`

## Routing Rules

1. **Zero `--help` or redundant pre-checks**: Use the exact flags in the table above on turn 1. Do NOT run `--help`, `status`, `tech-overview` (for today), or `estimate` before `slots` or `book` when the user already supplied the required inputs (e.g., RO, date, time, or duration).
2. **One-call `intake-slots` & when to run `slots`**: `intake-slots` returns filtered slots directly (no follow-up `slots` call needed). Run `slots` only when the user asks for available times, needs an equipment tag match (`--tags "Alignment Rack"`), or did not specify a date/time. Never invent slot times.
3. **Direct booking & conflict/ambiguity recovery**: When the user provides date and time (plus RO or provisional customer/vehicle), run `--dry-run book` / `book` directly. When the user supplies a date, time, and bay name (even an ambiguous name like `'Bay'`), do **NOT** pre-query `slots` before `--dry-run book` / `book` — pass the user's bay string directly to `--dry-run book` / `book` first and pick an `[Available]` matching bay from the 417 error. If `book` returns an ambiguous-bay 417 listing candidate bays with `[Available]` / `[Busy]` and equipment tags, retry `book` immediately with an `[Available]` matching bay (or run `slots --tags Lift --min-time <HH:MM>` then `book` if availability isn't shown). If a bay/tech conflict occurs, report the conflict, resource IDs, suggested alternative slots, and the returned deep link(s) from `urls` (conflicting entry / Calendar URL).
4. **Batch multi-step flows**: Combine 2+ calls (e.g., `unscheduled` + `slots`, or `status` + `tech-overview`) with `python -m tools.cli --json batch --cmd "..." --cmd "..."` (or `--file @cmds.json`) instead of PowerShell stdin pipes.

## Required Fields & Pre-Dispatch Callback

- **`create-ro-from-entry` required fields**: `--schedule-entry`, `--odometer-in` (`>= 0`; NEVER guess or omit), and vehicle **color** (`--color` / `--vehicle-color`) and **license plate** (`--license-plate` / `--plate`) if not already on the `Repair Vehicle` (customer self-serve `/book` appointments may arrive without a license plate, so supply `--license-plate` on RO conversion).
- **Human-facing vehicle references**: Refer to vehicles by **year, color, model, and license plate** (not VIN-only). Under tight space, drop VIN first, then year and plate (keep color + model).
- **`book` / `reschedule` / `appointment-status` required fields**: `--date` & `--time` (`book` also needs `--repair-order` or `--provisional-customer`); `--schedule-entry` + >= 1 change (`reschedule`); `--schedule-entry` + `--status` (`appointment-status`).
- **Pre-Dispatch Callback (`--dry-run`)**: Run `slots` / `status` / `--dry-run` first, output `### ISM Pre-Dispatch Summary` (marking any missing required fields `[MISSING — REQUIRED]`), and wait for explicit user confirmation before executing live writes.

## Verification

- Confirm `success: true`, a valid `SE-` ID, and `urls.calendar` / `urls.desk` in booking or reschedule output.
