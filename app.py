"""Run `python app.py --help` for the beginner-friendly command-line demo."""

import argparse
import json
import os

from agent import StructuredOutputAgent
from llm_client import MockClient, OpenAITicketClient
from logging_config import configure_logging
from tools import invalid_order_lookup, lookup_order

DEFAULT_MESSAGE = "I was charged twice for order ORD-1024. Please help me get a refund."


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate support tickets with Pydantic.")
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument("--provider", choices=("mock", "openai"), default="mock")
    parser.add_argument("--scenario", choices=MockClient.SCENARIOS, default="success")
    parser.add_argument("--max-attempts", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"))
    parser.add_argument("--log-file", default="logs/validation.jsonl")
    args = parser.parse_args()

    if args.provider == "openai" and args.scenario != "success":
        parser.error("--scenario is only available with --provider mock")
    if args.provider == "openai" and (
        not args.model or not os.getenv("OPENAI_API_KEY")
    ):
        parser.error("Live mode needs OPENAI_API_KEY and --model (or OPENAI_MODEL).")

    try:
        logger = configure_logging(args.log_file)
        client = MockClient(args.scenario) if args.provider == "mock" else OpenAITicketClient(args.model)
        tool = invalid_order_lookup if args.scenario == "invalid-tool" else lookup_order
        result = StructuredOutputAgent(
            client, tool=tool, max_attempts=args.max_attempts, logger=logger,
        ).run(args.message)
    except (OSError, ImportError):
        print(json.dumps({"status": "failed", "error_code": "setup_error",
                          "message": "Check installed dependencies and the log-file location."}))
        return 2

    print(result.model_dump_json(indent=2))
    return 1 if result.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
