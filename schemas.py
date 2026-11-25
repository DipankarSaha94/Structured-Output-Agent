"""Contracts shared by the model, tools, and Python controller."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Ticket(Contract):
    category: Literal["billing", "technical", "account", "other"]
    priority: Literal["low", "medium", "high"]
    order_id: str | None = Field(pattern=r"^ORD-[0-9]+$")
    summary: str = Field(min_length=10, max_length=300)
    needs_human_review: bool


class OrderLookupResult(Contract):
    order_id: str = Field(pattern=r"^ORD-[0-9]+$")
    status: Literal["paid", "pending", "cancelled", "not_found"]


class AgentResult(Contract):
    """The CLI always returns this envelope, even when processing fails."""

    request_id: str
    status: Literal["success", "needs_review", "failed"]
    attempts: int = Field(ge=0)
    ticket: Ticket | None = None
    order: OrderLookupResult | None = None
    error_code: str | None = None
    message: str
