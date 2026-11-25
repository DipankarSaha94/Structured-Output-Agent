import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent import StructuredOutputAgent
from app import DEFAULT_MESSAGE
from llm_client import MockClient, OpenAITicketClient, ProviderFailure
from logging_config import configure_logging
from schemas import Ticket
from tools import invalid_order_lookup


class SequenceClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.feedback = []

    def generate(self, message, feedback=None):
        self.feedback.append(feedback)
        return next(self.responses)


def payload(**changes):
    data = json.loads(MockClient().generate(DEFAULT_MESSAGE))
    data.update(changes)
    return json.dumps(data)


@pytest.mark.parametrize("changes", [
    {"priority": "urgent"}, {"needs_human_review": "true"},
    {"unknown": "unexpected"}, {"summary": "short"},
    {"order_id": "INVALID"},
])
def test_schema_rejects_invalid_data(changes):
    with pytest.raises(ValidationError):
        Ticket.model_validate_json(payload(**changes))


def test_schema_requires_nullable_field_to_be_present():
    data = json.loads(payload())
    del data["order_id"]
    with pytest.raises(ValidationError):
        Ticket.model_validate(data)


@pytest.mark.parametrize("scenario,attempts", [("success", 1), ("repair", 2), ("parse-error", 2)])
def test_validation_and_recovery(scenario, attempts):
    result = StructuredOutputAgent(MockClient(scenario)).run(DEFAULT_MESSAGE)
    assert result.attempts == attempts
    assert result.status == "needs_review"
    assert result.ticket.category == "billing"
    assert result.order.status == "paid"


def test_exhaustion_never_calls_tool():
    calls = []
    result = StructuredOutputAgent(
        MockClient("exhausted"), tool=lambda order_id: calls.append(order_id), max_attempts=2,
    ).run(DEFAULT_MESSAGE)
    assert result.status == "failed"
    assert result.error_code == "validation_exhausted"
    assert result.attempts == 2
    assert result.ticket is None
    assert calls == []


def test_retry_receives_safe_feedback():
    client = SequenceClient([payload(priority="urgent"), payload()])
    StructuredOutputAgent(client).run(DEFAULT_MESSAGE)
    assert client.feedback == [None, [{"field": "priority", "type": "literal_error"}]]


def test_invented_order_id_is_rejected_and_retried():
    client = SequenceClient([payload(order_id="ORD-9999"), payload()])
    result = StructuredOutputAgent(client).run(DEFAULT_MESSAGE)
    assert result.attempts == 2
    assert result.order.order_id == "ORD-1024"
    assert client.feedback[1][0]["type"] == "source_mismatch"


def test_omitted_source_order_id_is_rejected():
    result = StructuredOutputAgent(SequenceClient([payload(order_id=None)]), max_attempts=1).run(DEFAULT_MESSAGE)
    assert result.status == "failed"


def test_no_order_id_skips_tool():
    def unexpected_tool(order_id):
        pytest.fail("Tool must not run without an order ID")

    result = StructuredOutputAgent(MockClient(), tool=unexpected_tool).run("I cannot access my account")
    assert result.status == "success"
    assert result.ticket.order_id is None
    assert result.order is None


def test_multiple_order_ids_require_review():
    result = StructuredOutputAgent(MockClient()).run("Check ORD-1024 and ORD-1025")
    assert result.status == "needs_review"
    assert result.ticket.order_id is None


def test_billing_review_is_enforced_by_controller():
    result = StructuredOutputAgent(SequenceClient([payload(needs_human_review=False)])).run(DEFAULT_MESSAGE)
    assert result.status == "needs_review"
    assert result.ticket.needs_human_review is True


@pytest.mark.parametrize("tool,code", [
    (invalid_order_lookup, "invalid_tool_response"),
    (lambda order_id: {"order_id": "ORD-9999", "status": "paid"}, "tool_order_mismatch"),
])
def test_invalid_tool_result_is_not_accepted(tool, code):
    result = StructuredOutputAgent(MockClient(), tool=tool).run(DEFAULT_MESSAGE)
    assert result.status == "needs_review"
    assert result.error_code == code
    assert result.order is None
    assert result.attempts == 1


def test_tool_exception_is_controlled():
    def broken_tool(order_id):
        raise TimeoutError("private upstream details")

    result = StructuredOutputAgent(MockClient(), tool=broken_tool).run(DEFAULT_MESSAGE)
    assert result.error_code == "tool_error"
    assert result.status == "needs_review"
    assert "private" not in result.model_dump_json()


def test_unknown_order_requires_review():
    result = StructuredOutputAgent(MockClient()).run("Check my order ORD-9999")
    assert result.status == "needs_review"
    assert result.order.status == "not_found"


@pytest.mark.parametrize("scenario,code", [("refusal", "refused"), ("incomplete", "incomplete")])
def test_unusable_generation_is_not_retried(scenario, code):
    result = StructuredOutputAgent(MockClient(scenario)).run(DEFAULT_MESSAGE)
    assert result.status == "failed"
    assert result.error_code == code
    assert result.attempts == 1


@pytest.mark.parametrize("message", ["", "   ", "x" * 10001])
def test_bad_input_uses_no_model_attempts(message):
    result = StructuredOutputAgent(SequenceClient([])).run(message)
    assert result.error_code == "invalid_input"
    assert result.attempts == 0


def test_json_logs_exclude_invalid_values_and_unknown_field_names(tmp_path):
    secret = "private-customer-data"
    log_file = tmp_path / "validation.jsonl"
    logger = configure_logging(str(log_file))
    client = SequenceClient([payload(**{secret: secret}), payload()])
    result = StructuredOutputAgent(client, logger=logger).run(DEFAULT_MESSAGE)
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    raw = log_file.read_text(encoding="utf-8")
    events = [json.loads(line) for line in raw.splitlines()]
    assert secret not in raw
    assert DEFAULT_MESSAGE not in raw
    failure = next(event for event in events if event["event"] == "validation_failed")
    assert failure["request_id"] == result.request_id
    assert failure["errors"] == [{"field": "<object>", "type": "extra_forbidden"}]


@pytest.mark.parametrize("response,expected_error", [
    (SimpleNamespace(status="completed", output=[], output_text=payload()), None),
    (SimpleNamespace(status="incomplete", output=[], output_text=""), "incomplete"),
    (SimpleNamespace(status="completed", output=[SimpleNamespace(content=[
        SimpleNamespace(type="refusal")])], output_text=""), "refused"),
])
def test_live_adapter_with_fake_sdk(monkeypatch, response, expected_error):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return response

    class FakeOpenAIError(Exception):
        pass

    fake_sdk = SimpleNamespace(
        OpenAI=lambda **kwargs: SimpleNamespace(responses=SimpleNamespace(create=create)),
        OpenAIError=FakeOpenAIError,
    )
    monkeypatch.setitem(sys.modules, "openai", fake_sdk)
    client = OpenAITicketClient("test-model")
    if expected_error:
        with pytest.raises(ProviderFailure) as exc:
            client.generate(DEFAULT_MESSAGE)
        assert exc.value.code == expected_error
    else:
        assert Ticket.model_validate_json(client.generate(DEFAULT_MESSAGE)).category == "billing"
    assert calls[0]["store"] is False
    assert calls[0]["text"]["format"]["strict"] is True


def test_cli_exits_nonzero_with_structured_failure(tmp_path):
    completed = subprocess.run(
        [sys.executable, "app.py", "--scenario", "exhausted", "--log-file", str(tmp_path / "events.jsonl")],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["error_code"] == "validation_exhausted"
