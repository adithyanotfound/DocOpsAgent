"""Eval Harness Test Runner.

Scaffold for evaluating the document editor agent against the prompt list.
Currently prints the deterministic/subjective classification.
Future versions will actually run the agent end-to-end and evaluate the output.
"""
import asyncio
import logging
import sys
from pathlib import Path

# Adjust path so we can import from app
sys.path.append(str(Path(__file__).parent.parent.parent))

from tests.eval.prompts import PROMPT_LIST

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eval_runner")


async def run_eval() -> None:
    """Run the evaluation harness over all prompts."""
    log.info("Starting Eval Harness with %d prompts", len(PROMPT_LIST))
    
    passed_count = 0
    failed_count = 0
    skipped_count = 0

    for idx, eval_case in enumerate(PROMPT_LIST):
        prompt_id = eval_case["id"]
        prompt_text = eval_case["prompt"]
        prompt_type = eval_case["type"]
        assertion = eval_case["assertion"]
        
        log.info("---")
        log.info("Evaluating [%d/%d]: %s", idx + 1, len(PROMPT_LIST), prompt_id)
        log.info("Prompt: '%s'", prompt_text)
        log.info("Type: %s", prompt_type)
        
        if prompt_type == "deterministic":
            log.info("Assertion Schema: %s", assertion)
            # TODO: Run agent and check deterministic structural assertion
            log.info("Result: SKIPPED (End-to-end execution not fully wired in scaffold)")
            skipped_count += 1
        else:
            log.info("LLM Judge Criteria: '%s'", assertion)
            # TODO: Run agent and use LLM judge to verify output plausibility
            log.info("Result: SKIPPED (LLM Judge not fully wired in scaffold)")
            skipped_count += 1

    log.info("=========================================")
    log.info("Eval Run Complete")
    log.info("Passed:  %d", passed_count)
    log.info("Failed:  %d", failed_count)
    log.info("Skipped: %d", skipped_count)
    log.info("Total:   %d", len(PROMPT_LIST))


if __name__ == "__main__":
    asyncio.run(run_eval())
