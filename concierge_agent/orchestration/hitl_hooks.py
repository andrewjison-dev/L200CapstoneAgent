"""Human-in-the-Loop (HITL) Confirmation Gates for High-Stakes Actions.

Enforces explicit human confirmation stops BEFORE executing irreversible or financial
operations:
  1. Dispatching a paid grocery delivery/pickup order (`submit_grocery_cart_order_with_approval`).
  2. Writing Sunday prep & dinner blocks onto Andrew's shared family calendar
     (`publish_prep_blocks_to_family_calendar_with_approval`).

Integrates with ADK's `FunctionTool(..., require_confirmation=True)` and
`ToolContext.request_confirmation(...)` as well as deterministic approval tokens for the UI.
"""

from __future__ import annotations

from datetime import datetime, timezone
import secrets
from typing import Any

from ..observability.structured_logger import get_structured_logger

logger = get_structured_logger("concierge_agent.orchestration.hitl")


class HumanApprovalGate:
    """Stateful HITL confirmation gate managing pending high-stakes action requests."""

    def __init__(self) -> None:
        self._pending_requests: dict[str, dict[str, Any]] = {}
        self._approved_tokens: dict[str, dict[str, Any]] = {}

    def stage_for_approval(
        self,
        *,
        action_type: str,
        target_id: str,
        summary: str,
        estimated_cost_usd: float = 0.0,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Creates a pending HITL approval checkpoint and issues a unique `approval_token`."""
        approval_token = f"apr-{secrets.token_hex(8)}"
        record = {
            "approval_token": approval_token,
            "action_type": action_type,
            "target_id": target_id,
            "summary": summary,
            "estimated_cost_usd": round(estimated_cost_usd, 2),
            "status": "PENDING_HUMAN_APPROVAL",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "payload": payload or {},
        }
        self._pending_requests[approval_token] = record
        logger.info(
            f"HITL gate staged pending approval for {action_type} ({target_id})",
            extra={"structured_data": record},
        )
        return record

    def grant_human_approval(
        self,
        approval_token: str,
        *,
        approved_by: str = "Andrew",
    ) -> dict[str, Any]:
        """Marks a staged `approval_token` as explicitly approved by the human user."""
        pending = self._pending_requests.get(approval_token)
        if pending is None:
            raise ValueError(
                f"Unknown or expired approval_token '{approval_token}'. "
                "Re-stage the cart or calendar block to generate a fresh confirmation request."
            )
        pending["status"] = "APPROVED"
        pending["approved_by"] = approved_by
        pending["approved_at_utc"] = datetime.now(timezone.utc).isoformat()
        self._approved_tokens[approval_token] = pending
        logger.info(
            f"HITL approval granted by {approved_by} for {pending['action_type']}",
            extra={"structured_data": pending},
        )
        return pending

    def verify_execution_authorized(
        self,
        *,
        action_type: str,
        target_id: str,
        human_approval_confirmed: bool,
        approval_token: str | None,
        tool_context: Any | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Checks whether human confirmation has been granted; triggers ADK pause if not.

        Returns:
            `(True, None)` if execution is authorized, or `(False, pending_gate_record)`
            if execution MUST stop and wait for Andrew's confirmation.
        """
        # Support ADK native `ToolContext.tool_confirmation` when invoked via ADK Runner
        if tool_context is not None:
            tool_conf = getattr(tool_context, "tool_confirmation", None)
            if tool_conf is not None and getattr(tool_conf, "confirmed", False):
                return True, None

        if (
            human_approval_confirmed
            and approval_token
            and approval_token in self._approved_tokens
        ):
            approved_rec = self._approved_tokens[approval_token]
            if (
                approved_rec["action_type"] == action_type
                and approved_rec["target_id"] == target_id
            ):
                return True, None

        # Not yet approved -> request confirmation via ADK ToolContext if present and hard-stop
        existing = next(
            (
                rec
                for rec in self._pending_requests.values()
                if rec["action_type"] == action_type
                and rec["target_id"] == target_id
                and rec["status"] == "PENDING_HUMAN_APPROVAL"
            ),
            None,
        )
        if existing is None:
            existing = self.stage_for_approval(
                action_type=action_type,
                target_id=target_id,
                summary=f"Confirm high-stakes action '{action_type}' on '{target_id}'.",
            )

        if tool_context is not None and hasattr(tool_context, "request_confirmation"):
            try:
                tool_context.request_confirmation(
                    hint=(
                        f"Human approval required from Andrew before '{action_type}' "
                        f"on '{target_id}'."
                    ),
                    payload={"approval_token": existing["approval_token"]},
                )
            except Exception:
                pass

        return False, existing

    def list_pending_approvals(self) -> list[dict[str, Any]]:
        """Returns all currently unapproved HITL gates for the Command Center UI."""
        return [
            rec
            for rec in self._pending_requests.values()
            if rec["status"] == "PENDING_HUMAN_APPROVAL"
        ]


_DEFAULT_GATE: HumanApprovalGate | None = None


def get_approval_gate() -> HumanApprovalGate:
    """Returns the singleton HumanApprovalGate."""
    global _DEFAULT_GATE
    if _DEFAULT_GATE is None:
        _DEFAULT_GATE = HumanApprovalGate()
    return _DEFAULT_GATE
