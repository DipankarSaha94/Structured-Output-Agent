"""A small controller: generate -> validate -> repair -> validate tools -> return."""

import logging
from collections.abc import Callable
from uuid import uuid4

from pydantic import ValidationError

from llm_client import ProviderFailure, TicketClient, order_ids_in
from schemas import AgentResult, OrderLookupResult, Ticket
from tools import lookup_order


def validation_feedback(exc: ValidationError, stage: str) -> list[dict]:
    # Use only known field names. Never log arbitrary extra keys or invalid values.
    allowed = set(Ticket.model_fields if stage == "model" else OrderLookupResult.model_fields)
    return [
        {
            "field": str(error["loc"][0])
            if error["loc"] and error["loc"][0] in allowed else "<object>",
            "type": error["type"],
        }
        for error in exc.errors(include_input=False, include_url=False)
    ]


class StructuredOutputAgent:
    def __init__(
        self,
        client: TicketClient,
        tool: Callable[[str], dict] = lookup_order,
        max_attempts: int = 3,
        logger: logging.Logger | None = None,
    ):
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        self.client = client
        self.tool = tool
        self.max_attempts = max_attempts
        self.logger = logger or logging.getLogger("structured_output_agent")

    def run(self, message: str) -> AgentResult:
        request_id = str(uuid4())

        def event(name: str, stage: str, attempt: int, **details) -> None:
            self.logger.info(name, extra={"event_fields": {
                "request_id": request_id,
                "schema_version": "1.0",
                "stage": stage,
                "attempt": attempt,
                **details,
            }})

        def failed(code: str, attempt: int, description: str) -> AgentResult:
            event("request_failed", "controller", attempt, error_code=code)
            return AgentResult(
                request_id=request_id, status="failed", attempts=attempt,
                error_code=code, message=description,
            )

        if not message.strip() or len(message) > 10000:
            return failed("invalid_input", 0, "Supply a message of 1 to 10000 characters.")

        feedback = None
        for attempt in range(1, self.max_attempts + 1):
            event("generation_started", "model", attempt)
            try:
                raw_response = self.client.generate(message, feedback)
            except ProviderFailure as exc:
                return failed(exc.code, attempt, "Model did not return a usable completed response.")

            try:
                ticket = Ticket.model_validate_json(raw_response)
            except ValidationError as exc:
                feedback = validation_feedback(exc, "model")
                event("validation_failed", "model", attempt, errors=feedback)
                continue

            # Semantic rule checked against the source, independently of the LLM.
            ids = order_ids_in(message)
            expected_id = next(iter(ids)) if len(ids) == 1 else None
            if ticket.order_id != expected_id:
                feedback = [{"field": "order_id", "type": "source_mismatch"}]
                event("validation_failed", "business_rules", attempt, errors=feedback)
                continue

            # Conservative routing is application-owned; no automatic refunds.
            if ticket.category in ("billing", "other") or len(ids) > 1:
                ticket = ticket.model_copy(update={"needs_human_review": True})
            event("validation_passed", "model", attempt)
            break
        else:
            return failed("validation_exhausted", self.max_attempts,
                          "Output did not satisfy validation within the attempt limit.")

        order = None
        tool_error = None
        if ticket.order_id is not None:
            try:
                raw_order = self.tool(ticket.order_id)
            except Exception:
                # Tool failures are not model parse errors. Do not ask an LLM to fix them.
                tool_error = "tool_error"
                event("tool_failed", "tool", attempt, error_code=tool_error)
            else:
                try:
                    candidate = OrderLookupResult.model_validate(raw_order)
                except ValidationError as exc:
                    tool_error = "invalid_tool_response"
                    event("validation_failed", "tool", attempt,
                          errors=validation_feedback(exc, "tool"))
                else:
                    if candidate.order_id != ticket.order_id:
                        tool_error = "tool_order_mismatch"
                        event("validation_failed", "tool", attempt, error_code=tool_error)
                    else:
                        order = candidate
                        event("validation_passed", "tool", attempt)

        if tool_error or (order is not None and order.status == "not_found"):
            ticket = ticket.model_copy(update={"needs_human_review": True})

        needs_review = ticket.needs_human_review
        event("request_completed", "controller", attempt,
              status="needs_review" if needs_review else "success")
        return AgentResult(
            request_id=request_id,
            status="needs_review" if needs_review else "success",
            attempts=attempt, ticket=ticket, order=order, error_code=tool_error,
            message="Human review required; no action was taken." if needs_review
            else "Ticket validated; no action was taken.",
        )
