"""Evaluate OFFLINE controller fixtures, not live LLM accuracy or benchmarks."""

import json
import logging
from pathlib import Path

from agent import StructuredOutputAgent
from llm_client import MockClient


def main() -> None:
    logger = logging.Logger("offline_evaluation")
    logger.addHandler(logging.NullHandler())
    cases = json.loads((Path(__file__).parent / "examples/messages.json").read_text(encoding="utf-8"))
    for scenario in ("success", "repair", "parse-error", "exhausted"):
        results = [
            StructuredOutputAgent(MockClient(scenario), logger=logger).run(case["message"])
            for case in cases
        ]
        accepted = [result for result in results if result.ticket is not None]
        matching = sum(
            result.ticket is not None
            and result.ticket.category == case["expected_category"]
            and result.ticket.order_id == case["expected_order_id"]
            for result, case in zip(results, cases)
        )
        print(json.dumps({
            "mode": "offline_fixtures_only",
            "scenario": scenario,
            "cases": len(cases),
            "first_attempt_validation_rate": sum(r.attempts == 1 for r in accepted) / len(cases),
            "accepted_rate": len(accepted) / len(cases),
            "recovered_cases": sum(r.attempts > 1 for r in accepted),
            "controlled_failure_rate": sum(r.status == "failed" for r in results) / len(cases),
            "category_and_order_match_rate": matching / len(cases),
            "average_attempts": sum(r.attempts for r in results) / len(cases),
        }))


if __name__ == "__main__":
    main()
