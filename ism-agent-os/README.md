# ISM Agent OS

`ism-agent-os` supplies the skills, operational rules, CLI, Python client, and ingestion utilities that turn any AI agent platform into the operating system for a Frappe site with `ism` installed over direct Frappe REST (`/api/method/*` and `/api/resource/*`).

## Supported Agent Platforms

- **Google Antigravity**: Load as a workspace plugin via `.agents/skills.json`, or open directly as a workspace (`AGENTS.md`, `rules/AGENTS.md`, `skills/*/SKILL.md`).
- **Custom Agents**: Use `tools/cli.py` (`ism-agent-os`) or `tools/client.py` (`IsmAgentClient`).

## Quick Start

1. Copy `.env.example` to `.env` and enter your Frappe site URL and `Service Advisor` API credentials:

   ```bash
   cp .env.example .env
   ```

2. Run the zero-dependency REST connection diagnostic:

   ```bash
   python -m tools.cli test
   ```

3. Optional: install the CLI entrypoint (`ism-agent-os`) in editable mode:

   ```bash
   pip install -e . --no-build-isolation
   ```

## Included Shop Skills

- `skills/ism-service-advisor/SKILL.md`: Vehicle intelligence, check-in walkaround inspections, customer intake, draft quotes, quote amendments, and approvals.
- `skills/ism-shop-dispatcher/SKILL.md`: Dual-resource bay and technician scheduling, rescheduling, status updates, and ambient calendar sync.
- `skills/ism-parts-specialist/SKILL.md`: Structured supplier catalog imports, alternate lookups, Tesla manual extraction, and procurement.
- `skills/ism-floor-technician/SKILL.md`: Check-in inspections, shop floor transitions (`start`, `hold`, `park`, `complete`), supplements, and checkout.
- `skills/ism-shop-executive/SKILL.md`: Daily floor telemetry, bottleneck detection, action items, and WIP reporting.

## CLI & Direct Frappe REST

`tools/cli.py` (`ism-agent-os`) exposes curated shop subcommands (`quote`, `intake-slots`, `amend-quote`, `send-approval`, `slots`, `book`, `reschedule`, `appointment-status`, `create-ro-from-entry`, `estimate`, `status`, `cockpit`, `pnl-digest`, `publish-score`, `job-status`, `deposit`, `ingest-parts`, `price-part`, `alternates`, `fetch-tesla`, `tesla-generations`, `import-tesla`, `ingest-tesla`, `decode-vin`, `search-vehicles`, `vehicle-info`, `link-vehicle-customer`, `inspection-templates`, `apply-inspection`, `get-inspection`, `record-inspection`, `check-stock`, `procure`, `pay-purchase`, `purchase-invoice`, `tech-overview`, `unscheduled`, `receive-po`, `checkout`, `toggle-service`, `add-supplement`, `duplicate-side`, `deferred`, `add-deferred`, `action-items`, `action-cards`, `board-threads`, `board-thread`, `board-post`, `create-action`, `resolve-action`, `sync-action`, `stage-receipt`, `batch`) plus generic Frappe REST commands (`list-docs`, `get-doc`, `create-doc`, `update-doc`, `call-method`). Parts pricing rules and thresholds are configured in `rules/parts_pricing.json` and documented in `rules/PARTS_PRICING.md`.

All calls connect directly to Frappe REST endpoints (`/api/method/*` and `/api/resource/*`).
