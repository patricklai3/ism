"""Argparse subcommand builder for the ISM Agent OS CLI."""

from __future__ import annotations

import argparse


def _add_intake_args(parser: argparse.ArgumentParser) -> None:
	parser.add_argument("--customer", required=True, help="Customer name or ID")
	parser.add_argument("--vehicle", required=True, help="Vehicle ID, VIN, or license plate")
	parser.add_argument(
		"--service",
		action="append",
		help='Service title, "Title::hours" (e.g. "Tire Rotation::0.5"), or exact Service Package name (repeatable)',
	)
	parser.add_argument("--services-json", help='JSON array [{"service_title":"...","labor_hours":0.5}] (or @file.json)')
	parser.add_argument("--parts-json", help='JSON array [{"item_code":"...","qty":1}] (or @file.json)')
	parser.add_argument("--notes", help="Customer concern or advisor notes")
	parser.add_argument("--phone", help="Customer phone number")
	parser.add_argument("--email", help="Customer email address")
	parser.add_argument("--company", help="Company override")
	parser.add_argument("--odometer-in", type=int, help="Odometer reading at intake")


def _add_slot_filter_args(
	parser: argparse.ArgumentParser,
	duration_help: str = "Slot duration in minutes (default 60)",
) -> None:
	parser.add_argument("--date", help="Start date (ISO YYYY-MM-DD, defaults to today)")
	parser.add_argument("--days", type=int, default=3, help="Number of days to search (default 3)")
	parser.add_argument("--duration", type=int, help=duration_help)
	parser.add_argument(
		"--tags",
		"--equipment",
		action="append",
		help="Required equipment tag (e.g. Lift, Alignment Rack, HV Diagnostic; repeatable or comma-separated)",
	)
	parser.add_argument("--bay", help="Filter by Service Bay ID or name")
	parser.add_argument("--technician", "--tech", dest="technician", help="Filter by Technician Employee ID or name")
	parser.add_argument("--max-slots", type=int, help="Maximum number of slots to return")
	parser.add_argument("--min-time", help="Minimum 24-hour HH:MM[:SS] start time on start date")
	parser.add_argument("--include-past", action="store_true", help="Include elapsed slots on today's date")
	parser.add_argument("--include-resources", action="store_true", help="Include bay and technician directories")


def _build_parser(relax_required: bool = False) -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		prog="ism-agent-os",
		description="ISM Agent OS CLI for direct Frappe REST and Agent Facade operations.",
		formatter_class=argparse.RawDescriptionHelpFormatter,
		epilog=(
			"Examples:\n"
			"  ism-agent-os --json status --date 2026-11-15\n"
			"  ism-agent-os --json slots --date 2026-11-15 --duration 60 --tags Lift\n"
			"  ism-agent-os --json quote --customer 'Jane Doe' --vehicle 5YJ3E1EA1PF200101 --service 'Brake Service'\n"
			"  ism-agent-os --json book --repair-order RO-2026-00011 --date 2026-11-15 --time 09:00\n"
			"  ism-agent-os --json batch --cmd \"decode-vin --vin 5YJ3E1EA1PF200101\" --cmd \"vehicle-info --vehicle 5YJ3E1EA1PF200101\"\n"
			"  ism-agent-os --json batch --file @cmds.json"
		),
	)
	parser.add_argument("--env-file", help="Path to .env file")
	parser.add_argument("--json", action="store_true", help="Print raw JSON output")
	parser.add_argument("--summary", action="store_true", help="Request and print markdown summary_card")
	parser.add_argument(
		"--dry-run",
		"--preview",
		action="store_true",
		dest="dry_run",
		help="Validate arguments and output ISM Pre-Dispatch Summary without dispatching write calls",
	)
	parser.add_argument(
		"--stage",
		action="store_true",
		dest="stage",
		help="Stage write command(s) for local browser confirmation UI and output a 127.0.0.1 URL",
	)
	sub = parser.add_subparsers(dest="command", required=True)

	sub.add_parser("test", help="Verify Frappe REST and ISM Agent Facade connectivity")
	sub.add_parser("verify-connection", help="Alias for 'test' connectivity check")

	p_sr = sub.add_parser("stage-receipt", help="Read latest staged action receipt or inspect a specific stage_id")
	p_sr.add_argument("--id", dest="stage_id", help="Optional stage_id (e.g. stg_...) to inspect")

	p_batch = sub.add_parser("batch", help="Execute multiple CLI commands in one process")
	p_batch.add_argument(
		"input",
		nargs="?",
		help='JSON file path, @file.json, or inline JSON array (reads stdin if omitted)',
	)
	p_batch.add_argument("--file", "-f", dest="file", help="JSON file path or @file.json (overrides positional input)")
	p_batch.add_argument(
		"--cmd",
		"-c",
		action="append",
		dest="cmds",
		help='Command string to execute (repeatable, e.g. --cmd "status --date 2026-11-17" --cmd "tech-overview --date 2026-11-17")',
	)
	p_batch.add_argument("--stop-on-error", action="store_true", help="Stop executing remaining commands after the first error")

	_register_advisor_and_dispatch_parsers(sub)
	_register_floor_and_action_parsers(sub)
	_register_vehicle_and_inspection_parsers(sub)
	_register_parts_and_rest_parsers(sub)
	_register_board_parsers(sub)
	seen_parsers: set[int] = set()
	for cmd_name, sp in sub._name_parser_map.items():
		if id(sp) in seen_parsers:
			continue
		seen_parsers.add(id(sp))
		if relax_required and cmd_name not in ("batch", "test", "verify-connection", "stage-receipt"):
			for action in sp._actions:
				if action.option_strings:
					action.required = False
				elif action.nargs is None:
					action.nargs = "?"
			for grp in sp._mutually_exclusive_groups:
				grp.required = False
		sp.add_argument(
			"--dry-run",
			"--preview",
			action="store_true",
			dest="dry_run",
			default=argparse.SUPPRESS,
			help="Validate arguments and output ISM Pre-Dispatch Summary without dispatching write calls",
		)
		sp.add_argument(
			"--stage",
			action="store_true",
			dest="stage",
			default=argparse.SUPPRESS,
			help="Stage write command(s) for local browser confirmation UI and output a 127.0.0.1 URL",
		)
	return parser


def _register_advisor_and_dispatch_parsers(sub: argparse._SubParsersAction) -> None:
	sub.add_parser("status", help="Show live shop floor status").add_argument(
		"--date", help="Target date (ISO YYYY-MM-DD, defaults to today)"
	)
	sub.add_parser("tech-overview", help="Show daily technician utilization").add_argument(
		"--date", help="Target date (ISO YYYY-MM-DD, defaults to today)"
	)

	p_slots = sub.add_parser("slots", help="Find available appointment slots")
	p_slots.add_argument("--repair-order", help="Repair Order ID (e.g. RO-2026-00011)")
	_add_slot_filter_args(p_slots, duration_help="Slot duration in minutes (default 60)")

	p_resched = sub.add_parser("reschedule", help="Reschedule an existing Schedule Entry")
	p_resched.add_argument("--schedule-entry", required=True, help="Schedule Entry ID (e.g. SE-00007)")
	p_resched.add_argument("--date", help="New date (ISO YYYY-MM-DD)")
	p_resched.add_argument("--time", help="New start time (24-hour HH:MM or HH:MM:SS)")
	p_resched.add_argument("--duration", type=int, help="Updated duration in minutes")
	p_resched.add_argument("--bay", help="Service Bay ID or name")
	p_resched.add_argument("--tech", help="Technician Employee ID or name")
	p_resched.add_argument("--notes", help="Reschedule notes")

	p_astat = sub.add_parser("appointment-status", help="Update Schedule Entry status")
	p_astat.add_argument("--schedule-entry", required=True)
	p_astat.add_argument(
		"--status",
		required=True,
		choices=("Draft", "Scheduled", "Confirmed", "In Progress", "Completed", "No Show", "Cancelled"),
	)
	p_astat.add_argument("--notes")

	p_cro = sub.add_parser("create-ro-from-entry", help="Create Repair Order from pre-intake Schedule Entry")
	p_cro.add_argument("--schedule-entry", required=True)
	p_cro.add_argument("--odometer-in", type=int, help="Confirmed intake odometer reading")

	p_est = sub.add_parser("estimate", help="Estimate probabilistic job duration")
	p_est.add_argument("--repair-order")
	p_est.add_argument("--operations-json", help="JSON array of operation items or FRT dicts (or @file.json)")
	p_est.add_argument("--percentile", type=float)
	p_est.add_argument("--sigma", type=float)
	p_est.add_argument("--buffer-minutes", type=int)

	sub.add_parser("unscheduled", help="List unscheduled services on RO").add_argument("--repair-order", required=True)

	p_quote = sub.add_parser("quote", help="Create Draft Estimate Repair Order")
	_add_intake_args(p_quote)

	p_is = sub.add_parser("intake-slots", help="Create Draft Estimate Repair Order and find candidate slots in one call")
	_add_intake_args(p_is)
	_add_slot_filter_args(p_is, duration_help="Slot duration override in minutes")

	p_amend = sub.add_parser("amend-quote", help="Amend unapproved Repair Order")
	p_amend.add_argument("--repair-order", required=True)
	p_amend.add_argument(
		"--add-service",
		action="append",
		help='Service title or "Title::hours" e.g. "Wiper Blade Replacement::0.3" (repeatable)',
	)
	p_amend.add_argument("--add-services-json", help='JSON array [{"service_title":"...","labor_hours":0.5}] (or @file.json)')
	p_amend.add_argument("--remove-indices-json", help="JSON array of 0-based indices e.g. [0] (or @file.json)")
	p_amend.add_argument(
		"--update-services",
		help='JSON array of in-place service updates e.g. [{"idx":1,"labor_hours":1.5}] (or @file.json)',
	)
	p_amend.add_argument("--discount-amount", type=float, help="Order-level discount amount in CAD")
	p_amend.add_argument("--shop-supplies-amount", type=float, help="Shop supplies fee amount in CAD")
	p_amend.add_argument("--environmental-fee-amount", type=float, help="Environmental fee amount in CAD")
	p_amend.add_argument("--notes")

	p_auth = sub.add_parser("send-approval", help="Generate/send quote approval link (falls back to link if SMS unconfigured)")
	p_auth.add_argument("--repair-order", required=True)
	p_auth.add_argument("--medium", default="sms", choices=("sms", "email", "link"))
	p_auth.add_argument("--phone")
	p_auth.add_argument("--recipient")

	p_book = sub.add_parser("book", help="Book bay + technician Schedule Entry")
	p_book.add_argument("--repair-order", help="Repair Order ID (omit for standalone/provisional Diagnostic bookings)")
	p_book.add_argument("--date", required=True, help="Appointment date (ISO YYYY-MM-DD)")
	p_book.add_argument("--time", required=True, help="Start time (24-hour HH:MM or HH:MM:SS)")
	p_book.add_argument("--bay", help="Service Bay ID or name")
	p_book.add_argument("--tech", help="Technician Employee ID or name")
	p_book.add_argument("--entry-type", help="Schedule Entry Type (auto-inferred Repair/Diagnostic if omitted)")
	p_book.add_argument("--duration", type=int, help="Estimated duration in minutes (optional override)")
	p_book.add_argument("--provisional-customer", help="Walk-in / provisional customer name when booking without an RO")
	p_book.add_argument("--provisional-vehicle", help="Provisional vehicle summary when booking without an RO")
	p_book.add_argument("--notes", help="Booking notes")

	p_def = sub.add_parser("deferred", help="List open deferred services for a vehicle or Repair Order")
	p_def.add_argument("--repair-order")
	p_def.add_argument("--vehicle")

	p_adef = sub.add_parser("add-deferred", help="Add previously deferred services to a Repair Order")
	p_adef.add_argument("--repair-order", required=True)
	p_adef.add_argument("--item", action="append")
	p_adef.add_argument("--items-json")


def _register_floor_and_action_parsers(sub: argparse._SubParsersAction) -> None:
	p_job = sub.add_parser("job-status", help="Transition Repair Order job status")
	p_job.add_argument("--repair-order", required=True)
	p_job.add_argument("--action", required=True, choices=("start", "hold", "park", "complete"))
	p_job.add_argument("--odometer-out", type=int)
	p_job.add_argument("--notes")

	p_ts = sub.add_parser("toggle-service", help="Toggle completion of one or more service rows")
	p_ts.add_argument("--repair-order", required=True)
	p_ts.add_argument(
		"--service-row",
		action="extend",
		nargs="+",
		help="One or more service row names/indices (repeatable or space/comma-separated)",
	)
	p_ts.add_argument("--service-rows-json", help="JSON array of service row IDs (or @file.json)")
	p_ts.add_argument("--all", action="store_true", help="Toggle all active/approved service rows on the Repair Order")
	p_ts.add_argument("--reopen", action="store_true")

	p_sup = sub.add_parser("add-supplement", help="Add mid-job supplement for customer approval")
	p_sup.add_argument("--repair-order", required=True)
	p_sup.add_argument("--title", required=True)
	p_sup.add_argument("--category", default="Maintenance")
	p_sup.add_argument("--hours", type=float, default=1.0)
	p_sup.add_argument("--rate", type=float)
	p_sup.add_argument("--parts-json")
	p_sup.add_argument("--notes")

	p_dup = sub.add_parser("duplicate-side", help="Duplicate symmetrical LH/RH service for opposite side")
	p_dup.add_argument("--repair-order", required=True)
	p_dup.add_argument("--service-row", required=True)

	p_dep = sub.add_parser("deposit", help="Record a customer deposit payment on a Repair Order")
	p_dep.add_argument("repair_order", nargs="?", default=argparse.SUPPRESS, help="Repair Order ID (e.g. RO-2026-00011)")
	p_dep.add_argument("--repair-order", dest="repair_order", default=None, help="Repair Order ID (e.g. RO-2026-00011)")
	p_dep.add_argument("--amount", type=float, required=True, help="Deposit amount in CAD")
	p_dep.add_argument("--payment-method", default="Debit / Interac", help="Payment method (default: Debit / Interac)")
	p_dep.add_argument("--reference-no", help="Optional payment reference number")

	p_chk = sub.add_parser("checkout", help="Invoice, collect payment, and release Repair Order")
	p_chk.add_argument("--repair-order", required=True)
	p_chk.add_argument("--payment-method", help="Payment method (required unless --payments is given, e.g. 'Debit / Interac', 'Visa', 'Cash')")
	p_chk.add_argument("--paid-amount", type=float)
	p_chk.add_argument("--odometer-out", type=int)
	p_chk.add_argument(
		"--payments",
		help='JSON array of split-tender payments [{"payment_method":"Visa","amount":100}] (or @file.json)',
	)
	p_chk.add_argument("--confirm", action="store_true")

	p_lai = sub.add_parser("action-items", help="List Shop Action Items")
	p_lai.add_argument("--status", default="Open")
	p_lai.add_argument("--ref-doctype")
	p_lai.add_argument("--ref-name")
	p_lai.add_argument("--category")
	p_lai.add_argument("--limit", type=int, default=20)

	p_cai = sub.add_parser("create-action", help="Create Shop Action Item")
	p_cai.add_argument("--title", required=True)
	p_cai.add_argument("--category", default="Shop Floor Discrepancy")
	p_cai.add_argument("--priority", default="High")
	p_cai.add_argument("--ref-doctype")
	p_cai.add_argument("--ref-name")
	p_cai.add_argument("--item-code")
	p_cai.add_argument("--details")
	p_cai.add_argument("--dedup-key")
	p_cai.add_argument("--source", choices=("Manual", "Agent", "Rule Snooze"))

	p_rai = sub.add_parser("resolve-action", help="Resolve a Shop Action Item")
	p_rai.add_argument("--action-item", "--name", dest="action_item", required=True)
	p_rai.add_argument("--notes")

	p_sai = sub.add_parser("sync-action", help="Snooze, dismiss, or reopen a synced Shop Action Item key")
	p_sai.add_argument("--dedup-key", required=True)
	p_sai.add_argument("--action", required=True, choices=("snooze", "dismiss", "reopen"))
	p_sai.add_argument("--snooze-minutes", type=int)
	p_sai.add_argument("--title")
	p_sai.add_argument("--notes")
	p_sai.add_argument("--ref-doctype")
	p_sai.add_argument("--ref-name")

	p_cockpit = sub.add_parser("cockpit", help="Launch local Monitoring Cockpit URL or return computed JSON")
	p_cockpit.add_argument("--date", help="Snapshot date (ISO YYYY-MM-DD, defaults to today)")
	p_cockpit.add_argument("--data-only", action="store_true", help="Print computed gauges, queue, and swimlane JSON")

	p_pnl = sub.add_parser("pnl-digest", help="Fetch server-computed P&L and margin digest inputs")
	p_pnl.add_argument("--from-date", help="Start posting date (ISO YYYY-MM-DD)")
	p_pnl.add_argument("--to-date", help="End posting date (ISO YYYY-MM-DD)")
	p_pnl.add_argument("--company")

	p_pub = sub.add_parser("publish-score", help="Publish a synced 0-100 Shop Health Snapshot score")
	p_pub.add_argument("--metric", required=True, help="Metric key (e.g. pnl_health, tomorrow_readiness)")
	p_pub.add_argument("--score", type=int, required=True, help="Health score from 0 to 100")
	p_pub.add_argument("--verdict", required=True, help="Concise 1-line score driver")
	p_pub.add_argument("--grade", choices=("Healthy", "Watch", "Critical"))
	p_pub.add_argument("--period-date", help="Period date (ISO YYYY-MM-DD)")
	p_pub.add_argument("--report-url", help="Frappe Desk report deep link")
	p_pub.add_argument("--details-json", help="Optional JSON details object/array (or @file.json)")


def _register_vehicle_and_inspection_parsers(sub: argparse._SubParsersAction) -> None:
	sub.add_parser("decode-vin", help="Decode a 17-character Tesla VIN").add_argument("--vin", required=True)

	p_sv = sub.add_parser("search-vehicles", help="Search Repair Vehicles by VIN, plate, customer, or model")
	p_sv.add_argument("--query")
	p_sv.add_argument("--customer")
	p_sv.add_argument("--model")
	p_sv.add_argument("--limit", type=int, default=20)

	p_vi = sub.add_parser("vehicle-info", help="Fetch vehicle VIN decode, owners, history, and deferred work")
	p_vi.add_argument("--vehicle", required=True)
	p_vi.add_argument("--history-limit", type=int, default=20)

	p_lvc = sub.add_parser("link-vehicle-customer", help="Link a Customer to a Repair Vehicle")
	p_lvc.add_argument("--vehicle", required=True)
	p_lvc.add_argument("--customer", required=True)
	p_lvc.add_argument("--primary", action="store_true")
	p_lvc.add_argument("--relationship")

	p_it = sub.add_parser("inspection-templates", help="List check-in Inspection Templates or get one by name")
	p_it.add_argument("--template")
	p_it.add_argument("--include-inactive", action="store_true")

	p_ai = sub.add_parser("apply-inspection", help="Apply an Inspection Template to a Repair Order")
	p_ai.add_argument("--repair-order", required=True)
	p_ai.add_argument("--template")
	p_ai.add_argument("--replace", action="store_true")

	sub.add_parser("get-inspection", help="Read check-in inspection items and summary for a Repair Order").add_argument(
		"--repair-order", required=True
	)

	p_ri = sub.add_parser("record-inspection", help="Record check-in inspection findings, odometer, and concern")
	p_ri.add_argument("--repair-order", required=True)
	p_ri.add_argument("--items-json", help="JSON array of inspection item results (or @file.json)")
	p_ri.add_argument("--odometer-in", type=int)
	p_ri.add_argument("--concern")
	p_ri.add_argument("--template")
	p_ri.add_argument("--auto-apply", action="store_true")


def _register_parts_and_rest_parsers(sub: argparse._SubParsersAction) -> None:
	p_stk = sub.add_parser("check-stock", help="Check Repair Order or item list warehouse stock and deficits")
	p_stk.add_argument("--repair-order")
	p_stk.add_argument("--items-json", help="JSON array of item dicts/codes (or @file.json)")
	p_stk.add_argument("--company")

	p_po = sub.add_parser("procure", help="Create and submit POs/RFQs (standalone inventory or linked to an RO)")
	p_po.add_argument("--repair-order")
	p_po.add_argument("--parts-json", required=True)
	p_po.add_argument("--company")
	p_po.add_argument("--draft", action="store_true", help="Keep created POs/RFQs in Draft instead of submitting")
	p_po.add_argument("--submit", action="store_true", help="Deprecated no-op alias (submitting is now the default)")
	p_po.add_argument("--payment-method", help="Submit an advance Payment Entry against each created PO")
	p_po.add_argument("--paid-amount", type=float, help="Advance amount paid (defaults to PO grand_total)")
	p_po.add_argument("--reference-no", help="Bank/cheque/card transaction reference")
	p_po.add_argument("--reference-date", help="Transaction reference date (YYYY-MM-DD)")

	p_rcv = sub.add_parser("receive-po", help="Submit PO if Draft and post Purchase Receipt for PO or un-received Purchase Invoice")
	p_rcv.add_argument("--purchase-order")
	p_rcv.add_argument("--purchase-invoice")
	p_rcv.add_argument("--submit-po", action="store_true", help="Deprecated no-op alias (submitting is now the default)")

	p_pay = sub.add_parser("pay-purchase", help="Submit a supplier Payment Entry against a Purchase Order or Purchase Invoice")
	p_pay.add_argument("--purchase-order")
	p_pay.add_argument("--purchase-invoice")
	p_pay.add_argument("--payment-method")
	p_pay.add_argument("--paid-amount", type=float)
	p_pay.add_argument("--reference-no")
	p_pay.add_argument("--reference-date")

	p_pinv = sub.add_parser("purchase-invoice", help="Create and submit a Purchase Invoice (attaching PO advances, optionally updating stock)")
	p_pinv.add_argument("--purchase-order")
	p_pinv.add_argument("--supplier")
	p_pinv.add_argument("--parts-json")
	p_pinv.add_argument("--update-stock", action="store_true", help="Check Update Stock when invoice arrives with physical goods")
	p_pinv.add_argument("--bill-no")
	p_pinv.add_argument("--bill-date")
	p_pinv.add_argument("--company")
	p_pinv.add_argument("--no-attach-advances", action="store_true", help="Do not pull advance payments from linked Purchase Order")

	p_parts = sub.add_parser("ingest-parts", help="Sanitize and import supplier catalog file as structured JSON")
	p_parts.add_argument("file")
	p_parts.add_argument("--supplier")
	p_parts.add_argument("--source", default="Aftermarket")
	p_parts.add_argument("--condition", default="New")
	p_parts.add_argument("--service-package", help="Target Service Package name(s) to nest ingested parts under (required)")
	p_parts.add_argument("--tesla-price", type=float, help="Optional uniform Tesla Canada OEM price (CAD) for single-part imports")
	p_parts.add_argument("--preserve-rate", action="store_true", help="Keep explicit rate column values instead of recalculating from Tesla OEM price")
	p_parts.add_argument("--sync-rates", action="store_true", help="Update Item.standard_rate in Frappe for existing items after import")
	p_parts.add_argument("--pricing-config", help="Optional path to custom parts_pricing.json config")

	p_price = sub.add_parser("price-part", help="Calculate CAD selling price from Tesla OEM price and cost rules")
	p_price.add_argument("--tesla-price", type=float, help="Verified Tesla Canada OEM price in CAD")
	p_price.add_argument("--cost", type=float, help="Purchase cost in CAD")
	p_price.add_argument("--source", default="Aftermarket", help="Part source: OEM or Aftermarket")
	p_price.add_argument("--condition", default="New", help="Part condition: New, Used, or Reconditioned")
	p_price.add_argument("--part-number", help="OEM or supplier part number (used to build Tesla search URL)")
	p_price.add_argument("--part-name", help="Part description/name (used to build Tesla search URL if no part number)")
	p_price.add_argument("--override-rate", type=float, help="Explicit manual selling rate override in CAD")
	p_price.add_argument("--config", help="Optional path to custom parts_pricing.json config")

	p_alt = sub.add_parser("alternates", help="Find compatible OEM, aftermarket, or used alternate parts")
	p_alt.add_argument("--service-package")
	p_alt.add_argument("--component-slot")
	p_alt.add_argument("--slot-number", type=int)
	p_alt.add_argument("--oem")
	p_alt.add_argument("--item-code")

	p_ft = sub.add_parser("fetch-tesla", help="Fetch/parse Tesla manual URL")
	p_ft.add_argument("url")
	p_ft.add_argument("--repair-order")
	p_ft.add_argument("--vehicle")

	p_imp_t = sub.add_parser("import-tesla", help="Create/update Service Package or import procedure onto Repair Order")
	p_imp_t.add_argument("--title")
	p_imp_t.add_argument("--code")
	p_imp_t.add_argument("--hours", type=float, default=1.0)
	p_imp_t.add_argument("--model", default="Model 3")
	p_imp_t.add_argument("--parts-json")
	p_imp_t.add_argument("--repair-order")
	p_imp_t.add_argument("--url")
	p_imp_t.add_argument("--data-json", help="Parsed Tesla procedure JSON dict (or @file.json)")
	p_imp_t.add_argument("--on-conflict", default="skip", choices=("skip", "overwrite", "add", "error"))
	p_imp_t.add_argument("--force", action="store_true")
	p_imp_t.add_argument("--side")
	p_imp_t.add_argument("--supplement", action="store_true")
	p_imp_t.add_argument("--notes")

	p_tesla = sub.add_parser("ingest-tesla", help="Fetch and import a Tesla service manual URL")
	p_tesla.add_argument("url")
	p_tesla.add_argument("--model")
	p_tesla.add_argument("--repair-order")
	p_tesla.add_argument("--on-conflict", default="skip", choices=("skip", "overwrite", "add", "error"))
	p_tesla.add_argument("--force", action="store_true")
	p_tesla.add_argument("--side")

	p_ld = sub.add_parser("list-docs", help="Query any Frappe DocType via direct REST")
	p_ld.add_argument("--doctype", required=True)
	p_ld.add_argument("--filters-json", "--filters", dest="filters_json", help="JSON filters array (or @file.json)")
	p_ld.add_argument("--filter", action="append", help="Filter shorthand key=val or key=op:val (repeatable)")
	p_ld.add_argument("--fields-json", "--fields", dest="fields_json", help="JSON array, @file.json, or comma-separated fields")
	p_ld.add_argument("--limit", type=int, default=20)
	p_ld.add_argument("--order-by")

	p_gd = sub.add_parser("get-doc", help="Fetch a single Frappe document via direct REST")
	p_gd.add_argument("--doctype", required=True)
	p_gd.add_argument("--name", required=True)

	p_cd = sub.add_parser("create-doc", help="Create a Frappe document via direct REST")
	p_cd.add_argument("--doctype", required=True)
	p_cd.add_argument("--data-json", "--data", dest="data_json", required=True)

	p_ud = sub.add_parser("update-doc", help="Update a Frappe document via direct REST")
	p_ud.add_argument("--doctype", required=True)
	p_ud.add_argument("--name", required=True)
	p_ud.add_argument("--data-json", "--data", dest="data_json", required=True)

	p_cm = sub.add_parser("call-method", help="Call any whitelisted Frappe method via direct REST")
	p_cm.add_argument("--method", required=True)
	p_cm.add_argument("--args-json", "--args", dest="args_json")


def _register_board_parsers(sub: argparse._SubParsersAction) -> None:
	p_bts = sub.add_parser("board-threads", help="List Shop Board threads")
	p_bts.add_argument(
		"--thread-type",
		choices=("REPAIR_ORDER", "INTAKE", "AP_RECEIVING", "DISPATCH"),
		help="Filter by board thread type",
	)
	p_bts.add_argument(
		"--status",
		choices=("ACTIVE", "BLOCKED_PARTS", "BLOCKED_DECISION", "WAITING_CUSTOMER", "CLOSED"),
		help="Filter by board thread status",
	)
	p_bts.add_argument("--assigned-only", action="store_true", help="Only return threads assigned to the current user")
	p_bts.add_argument("--query", help="Search thread keys, titles, or repair orders")
	p_bts.add_argument("--limit", type=int, default=50, help="Maximum threads to return (default 50)")

	p_bt = sub.add_parser("board-thread", help="Fetch a single Shop Board thread and its event timeline")
	grp_bt = p_bt.add_mutually_exclusive_group(required=True)
	grp_bt.add_argument("--thread-key", help="Generalized thread key (e.g. RO:RO-2026-00011 or SHOP:INTAKE)")
	grp_bt.add_argument("--repair-order", help="Repair Order ID (resolves to RO:<id>)")
	p_bt.add_argument("--limit", type=int, default=100, help="Maximum events to return (default 100)")
	p_bt.add_argument("--before-event", help="Paginate events older than this Shop Board Event ID")

	p_bp = sub.add_parser("board-post", help="Post a text capture onto a Shop Board thread")
	grp_bp = p_bp.add_mutually_exclusive_group(required=True)
	grp_bp.add_argument("--thread-key", help="Generalized thread key (e.g. RO:RO-2026-00011 or SHOP:INTAKE)")
	grp_bp.add_argument("--repair-order", help="Repair Order ID (resolves to RO:<id>)")
	p_bp.add_argument("--text", required=True, help="Capture text to post onto the thread")
	p_bp.add_argument("--intent", help="Optional quick-intent tag")
	p_bp.add_argument("--idempotency-key", help="Optional idempotency key for safe retries")

	p_br = sub.add_parser("board-resolve", help="Resolve a pending Shop Board clarification or approval card")
	p_br.add_argument("--event", required=True, help="Shop Board Event ID (e.g. BE-...)")
	p_br.add_argument("--decision", required=True, choices=("approve", "reject", "clarify"), help="Card resolution decision")
	p_br.add_argument("--option", help="Selected option key or label")
	p_br.add_argument("--edited-message", help="Edited customer-facing message override")
	p_br.add_argument("--notes", help="Optional resolution notes")
	p_br.add_argument("--idempotency-key", help="Optional idempotency key for safe retries")

	p_bu = sub.add_parser("board-undo", help="Undo an AI receipt event on a Shop Board thread")
	p_bu.add_argument("--event", required=True, help="Shop Board Event ID of the AI receipt to undo")
	p_bu.add_argument("--reason", help="Optional undo reason")
	p_bu.add_argument("--idempotency-key", help="Optional idempotency key for safe retries")
