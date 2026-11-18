"""Command Center Web Server (FastAPI + Zero-Dependency Stdlib HTTP Fallback).

Serves Andrew's minimalist Family Meal Command Center UI (`static/index.html`) and JSON
endpoints driving intake check-ins, multi-agent orchestration, HITL grocery approval,
Cook-Mode timelines, async feedback memory, and AgentOps telemetry.
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..agent import ConciergeCommandCenterOrchestrator
from ..memory.async_memory_worker import get_async_memory_worker
from ..memory.persistent_store import get_memory_store
from ..observability.pii_redactor import get_pii_redactor
from ..observability.structured_logger import (
    get_audit_logger,
    get_structured_logger,
)
from ..observability.tracing import get_tracer
from ..orchestration.hitl_hooks import get_approval_gate
from ..orchestration.model_router import get_model_router
from ..tools import (
    collect_weekly_blocker_inputs_for_andrew,
    fetch_family_dietary_profile_and_schedule,
    generate_step_by_step_cooking_timeline,
    persist_weekly_family_meal_feedback,
    submit_grocery_cart_order_with_approval,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = get_structured_logger("concierge_agent.ui.server")


class CommandCenterAPI:
    """Backend controller for the Family Meal Command Center UI."""

    def __init__(self) -> None:
        self.orchestrator = ConciergeCommandCenterOrchestrator()

    def get_initial_state(self) -> dict[str, Any]:
        """Returns household profile, saved weekly check-in defaults, and active plan/cart."""
        profile_bundle = fetch_family_dietary_profile_and_schedule()
        store = get_memory_store()
        return {
            "status": "success",
            "family_profile": profile_bundle["family_profile"],
            "recalled_memories": profile_bundle["recalled_memories"],
            "latest_meal_plan": store.get_state("latest_meal_plan"),
            "latest_grocery_cart": store.get_state("latest_grocery_cart"),
            "pending_approvals": get_approval_gate().list_pending_approvals(),
        }

    def check_weekly_inputs(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Runs the Intake-First blocker check before planning."""
        return collect_weekly_blocker_inputs_for_andrew(
            week_start_date=str(payload.get("week_start_date", "2026-11-23")),
            late_work_or_travel_days=payload.get("late_work_or_travel_days") or [],
            sunday_prep_minutes_available=payload.get("sunday_prep_minutes_available"),
            weekly_budget_usd=payload.get("weekly_budget_usd"),
            workout_focus=payload.get("workout_focus"),
            use_up_fridge_items=payload.get("use_up_fridge_items") or [],
            kid_or_spouse_cravings=payload.get("kid_or_spouse_cravings") or [],
            use_standard_defaults=bool(payload.get("use_standard_defaults", False)),
        )

    def orchestrate_week(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Executes the full multi-agent orchestration pipeline once Andrew's inputs are ready."""
        session_id = str(payload.get("session_id", "ui-command-center-session"))
        user_msg = str(
            payload.get(
                "user_message",
                "Orchestrate my family meal plan, Sunday prep, grocery cart, and cook timelines.",
            )
        )
        return self.orchestrator.handle_command_center_turn(
            session_id=session_id,
            user_message=user_msg,
            checkin_inputs=payload,
        )

    def approve_and_submit_cart(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Grants explicit HITL confirmation from Andrew and dispatches the staged cart."""
        cart_id = str(payload.get("cart_id", ""))
        approval_token = str(payload.get("approval_token", ""))
        slot = str(
            payload.get(
                "delivery_or_pickup_slot",
                "Sunday 07:30-08:30 CST Curbside Pickup",
            )
        )
        if approval_token:
            get_approval_gate().grant_human_approval(approval_token, approved_by="Andrew")
        return submit_grocery_cart_order_with_approval(
            cart_id=cart_id,
            delivery_or_pickup_slot=slot,
            human_approval_confirmed=bool(approval_token),
            approval_token=approval_token or None,
        )

    def get_cooking_timeline(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Builds a step-by-step Cook Mode card for any selected recipe."""
        return generate_step_by_step_cooking_timeline(
            recipe_id=str(payload.get("recipe_id", "dinner_honey_garlic_salmon_bites")),
            target_serve_time_cst=str(payload.get("target_serve_time_cst", "17:45 CST")),
            sunday_prep_completed=bool(payload.get("sunday_prep_completed", True)),
        )

    def submit_meal_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Queues non-blocking async preference consolidation."""
        return persist_weekly_family_meal_feedback(
            recipe_id=str(payload.get("recipe_id", "")),
            andrew_rating=int(payload.get("andrew_rating", 5)),
            spouse_rating=int(payload.get("spouse_rating", 5)),
            kids_ate_cleanly=bool(payload.get("kids_ate_cleanly", True)),
            notes=str(payload.get("notes", "")),
        )

    def get_telemetry_snapshot(self) -> dict[str, Any]:
        """Returns live AgentOps observability data (routing, spans, intent/outcome, PII, async jobs)."""
        return {
            "status": "success",
            "model_routing_decisions": get_model_router().get_routing_history(limit=15),
            "recent_otel_spans": get_tracer().get_recent_spans(limit=25),
            "intent_outcome_audit": get_audit_logger().get_recent_entries(limit=20),
            "pii_redactions_total": get_pii_redactor().total_redactions,
            "async_memory_jobs": get_async_memory_worker().list_recent_jobs(limit=10),
        }


def _make_handler(api: CommandCenterAPI) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            logger.info("HTTP %s", fmt % args)

        def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
            raw = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _serve_static(self, rel_path: str) -> None:
            clean = "index.html" if rel_path in {"", "/"} else rel_path.lstrip("/")
            target = (STATIC_DIR / clean).resolve()
            if not str(target).startswith(str(STATIC_DIR)) or not target.is_file():
                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return
            mime, _ = mimetypes.guess_type(str(target))
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{mime or 'text/plain'}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/state":
                self._send_json(api.get_initial_state())
            elif parsed.path == "/api/telemetry":
                self._send_json(api.get_telemetry_snapshot())
            elif parsed.path == "/healthz":
                self._send_json({"status": "ok"})
            else:
                self._serve_static(parsed.path)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                payload = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                self._send_json({"status": "error", "message": "Invalid JSON"}, status=400)
                return

            routes = {
                "/api/checkin": api.check_weekly_inputs,
                "/api/orchestrate": api.orchestrate_week,
                "/api/approve-cart": api.approve_and_submit_cart,
                "/api/cook-timeline": api.get_cooking_timeline,
                "/api/feedback": api.submit_meal_feedback,
            }
            fn = routes.get(parsed.path)
            if fn is None:
                self._send_json({"error": "Unknown endpoint"}, status=404)
                return
            self._send_json(fn(payload))

    return _Handler


def run_standalone_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Starts the Family Meal Command Center HTTP server."""
    api = CommandCenterAPI()
    server = ThreadingHTTPServer((host, port), _make_handler(api))
    logger.info(
        f"Family Meal Command Center listening on http://{host}:{port}",
        extra={"structured_data": {"host": host, "port": port}},
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run Andrew's Family Meal Command Center UI"
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    run_standalone_server(host=args.host, port=args.port)
