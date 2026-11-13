# ISM Agent OS

`ism-agent-os` supplies the skills, operational rules, MCP server, and ingestion utilities that turn any AI agent platform into the operating system for a Frappe site with `ism` installed.

## Supported Agent Platforms

- **Google Antigravity**: Load as a workspace plugin via `.agents/plugins.json`, or open directly as a workspace.
- **Claude Code / Claude Desktop**: Uses `CLAUDE.md`, `.mcp.json`, and `claude_desktop_config.json`.
- **Cursor / Windsurf**: Uses `.cursorrules`, `.windsurfrules`, and `cursor_mcp.json`.
- **Open WebUI / Custom Agents**: Import `openwebui_tools.py` or use `tools/client.py` (`IsmAgentClient`).

## Quick Start

1. Copy `.env.example` to `.env` and enter your Frappe site URL and `Service Advisor` API credentials:

   ```bash
   cp .env.example .env
   ```

2. Run the zero-dependency connection diagnostic:

   ```bash
   python tools/ism_mcp_server.py --test
   ```

3. Optional: install the CLI entrypoints (`ism-agent-os` and `ism-mcp`) in editable mode:

   ```bash
   pip install -e . --no-build-isolation
   ```

## Included Shop Skills

- `skills/ism-service-advisor/SKILL.md`: Customer intake, draft quotes, quote amendments, and SMS approvals.
- `skills/ism-shop-dispatcher/SKILL.md`: Dual-resource bay and technician scheduling and ambient calendar updates.
- `skills/ism-parts-specialist/SKILL.md`: Supplier CSV catalog imports, Tesla manual extraction, and procurement.
- `skills/ism-floor-technician/SKILL.md`: Shop floor status transitions (`start`, `hold`, `complete`) with authorization locks.
- `skills/ism-shop-executive/SKILL.md`: Daily floor telemetry, bottleneck detection, and WIP reporting.

## Exposed MCP Tools

`tools/ism_mcp_server.py` exposes 14 curated ISM shop tools (`ism_create_quote`, `ism_amend_quote`, `ism_send_quote_for_approval`, `ism_find_available_slots`, `ism_book_appointment`, `ism_get_shop_floor_status`, `ism_update_job_status`, `ism_import_supplier_catalog`, `ism_import_tesla_procedure`, `ism_check_parts_stock`, `ism_submit_procurement_orders`, `ism_fetch_tesla_manual`, `ism_get_tech_overview`, `ism_get_unscheduled_services`) plus 5 generic Frappe tools (`frappe_list_documents`, `frappe_get_document`, `frappe_create_document`, `frappe_update_document`, `frappe_call_method`).

It connects over Frappe v16 native `/api/v2/mcp` and falls back to `/api/method/*` and `/api/resource/*` REST endpoints when needed.
