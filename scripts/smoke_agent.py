"""Assert the agent picks the right tools for the brief's scenarios.

    python scripts/smoke_agent.py
    python scripts/smoke_agent.py --verbose     # print full answers

This is the acceptance test for "appropriate tool selection based on user
intent": each scenario declares which tools MUST fire and which MUST NOT.
A knowledge-base question that triggers a weather call is a failure even if the
answer reads well.
"""

from __future__ import annotations

import sys as _sys

# The assistant's provenance labels are emoji; a Windows cp1252 console would
# raise UnicodeEncodeError when printing them.
for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import argparse
import asyncio
import logging
import sys

from app import agent as agent_module

#: Seconds to wait between scenarios.
#:
#: Groq's free tier allows 8,000 tokens per minute, and one combined scenario
#: makes several model calls that each resend the whole context -- enough to
#: consume most of a minute's budget on its own. Waiting just over a minute
#: gives each scenario a fresh window, so this suite measures the agent rather
#: than the rate limiter. It makes a full run take ~10 minutes.
PACE_SECONDS = 65.0

#: Substring of the agent's own rate-limit message. A scenario that never
#: reached the model proves nothing about tool selection, so it is reported as
#: SKIPPED rather than counted as a behavioural failure -- otherwise an
#: exhausted free-tier quota looks identical to a broken agent.
RATE_LIMIT_MARKER = "rate-limited right now"

WEATHER_TOOLS = {"get_current_weather", "get_weather_forecast"}
CURRENCY_TOOLS = {"convert_currency", "get_exchange_rate"}
KB_TOOL = "search_travel_knowledge_base"

SCENARIOS = [
    {
        "label": "RAG only -- destination facts",
        "question": "What are the must-visit attractions in Singapore?",
        "must_call": {KB_TOOL},
        "must_not_call": WEATHER_TOOLS | CURRENCY_TOOLS,
    },
    {
        "label": "MCP only -- current weather",
        "question": "What is the weather in Singapore right now?",
        "must_call": {"get_current_weather"},
        "must_not_call": CURRENCY_TOOLS,
    },
    {
        "label": "MCP only -- currency conversion",
        "question": "Convert INR 50,000 to SGD.",
        "must_call": {"convert_currency"},
        "must_not_call": WEATHER_TOOLS,
    },
    {
        "label": "COMBINED -- the brief's required scenario",
        "question": (
            "Create a three-day Singapore itinerary for next week and adjust it "
            "according to the weather forecast."
        ),
        "must_call": {KB_TOOL, "get_weather_forecast"},
        "must_not_call": set(),
    },
    {
        "label": "COMBINED -- budget plus itinerary",
        "question": (
            "I have a budget of INR 60,000. Convert it to SGD and suggest a "
            "three-day itinerary."
        ),
        "must_call": {KB_TOOL, "convert_currency"},
        "must_not_call": set(),
    },
    {
        "label": "Missing knowledge must be admitted",
        "question": "What are the best ski resorts in Singapore?",
        "must_call": {KB_TOOL},
        "must_not_call": WEATHER_TOOLS | CURRENCY_TOOLS,
    },
]


def called_tools(provenance: agent_module.Provenance) -> set[str]:
    return {call["tool"] for call in provenance.tool_calls}


async def main(verbose: bool, only_multiturn: bool = False) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    failures: list[str] = []
    skipped: list[str] = []

    print("building agent...")
    travel_agent = await agent_module.build_agent()
    description = travel_agent.describe()
    print(f"model: {description['model']}")
    print(f"tools: {', '.join(description['tools'])}")
    if description["degraded_tools"]:
        print(f"DEGRADED: {description['degraded_tools']}")
        failures.append("agent started with degraded tools; results are not comparable")
    print()

    for index, scenario in enumerate([] if only_multiturn else SCENARIOS,
                                      start=1):
        print("=" * 78)
        print(f"{index}. {scenario['label']}")
        print(f"   Q: {scenario['question']}")
        session = f"smoke-{index}"
        if index > 1:
            await asyncio.sleep(PACE_SECONDS)
        answer, provenance = await agent_module.ask(
            travel_agent, session, scenario["question"]
        )
        if RATE_LIMIT_MARKER in answer:
            print("   verdict      : SKIPPED -- provider rate limit, never "
                  "reached the model")
            skipped.append(scenario["label"])
            print()
            continue

        used = called_tools(provenance)

        missing = scenario["must_call"] - used
        forbidden = scenario["must_not_call"] & used
        ok = not missing and not forbidden

        print(f"   tools called : {sorted(used) or 'none'}")
        if missing:
            print(f"   MISSING      : {sorted(missing)}")
            failures.append(f"{scenario['label']}: did not call {sorted(missing)}")
        if forbidden:
            print(f"   FORBIDDEN    : {sorted(forbidden)}")
            failures.append(f"{scenario['label']}: wrongly called {sorted(forbidden)}")
        print(f"   kb sources   : {len(provenance.kb_sources)}")
        for call in provenance.tool_calls:
            status = "ok" if call["ok"] else f"FAILED: {call.get('error', '')[:60]}"
            print(f"     - {call['tool']}: {status}"
                  + (f" | {call.get('detail')}" if call.get("detail") else ""))
        print(f"   verdict      : {'OK' if ok else 'FAIL'}")
        if verbose:
            print()
            print("   " + "\n   ".join(answer.splitlines()))
        else:
            preview = " ".join(answer.split())[:220]
            print(f"   answer       : {preview}...")
        print()

    print("=" * 78)
    print("MULTI-TURN CONTEXT -- a preference stated once must persist")
    print("=" * 78)
    session = "smoke-multiturn"
    turns = [
        "I'm travelling to Singapore with two young children.",
        "We're on a tight budget too.",
        "Now build me a two-day plan.",
    ]
    for turn in turns:
        print(f"\n   Q: {turn}")
        answer, provenance = await agent_module.ask(travel_agent, session, turn)
        print(f"   tools: {sorted(called_tools(provenance)) or 'none'}")
        preview = " ".join(answer.split())[:260]
        print(f"   A: {preview}...")
        final_answer = answer

    if RATE_LIMIT_MARKER in final_answer:
        skipped.append("multi-turn context")
        print()
        print("   SKIPPED -- provider rate limit during the multi-turn check")
        print()
        print("=" * 78)
        return _report(failures, skipped)

    lowered = final_answer.lower()
    kid_words = ["child", "kid", "family", "young"]
    budget_words = ["budget", "cheap", "afford", "free", "inexpensive", "low-cost"]
    kept_kids = any(word in lowered for word in kid_words)
    kept_budget = any(word in lowered for word in budget_words)
    print()
    print(f"   retained 'two young children' without restating: {kept_kids}")
    print(f"   retained 'tight budget' without restating      : {kept_budget}")
    if not kept_kids:
        failures.append("multi-turn: lost the children preference")
    if not kept_budget:
        failures.append("multi-turn: lost the budget preference")

    stored = agent_module.history(travel_agent, session)
    users = [m for m in stored if m["role"] == "human"]
    print(f"   stored messages: {len(stored)} ({len(users)} user turns)")
    # Assert on the user turns, not the total: if a turn fails provider-side,
    # the user message is still checkpointed while the answer is not, and that
    # is the behaviour we want -- a failed turn must not lose what the user said.
    if len(users) != len(turns):
        failures.append(
            f"multi-turn: {len(turns)} user turns sent but {len(users)} stored"
        )

    print()
    print("=" * 78)
    return _report(failures, skipped)


def _report(failures: list[str], skipped: list[str]) -> int:
    if skipped:
        print(f"{len(skipped)} scenario(s) SKIPPED because the LLM provider "
              "refused the request:")
        for label in skipped:
            print(f"  - {label}")
        print()
        print("Groq's free tier caps tokens per day (200,000) and per minute "
              "(8,000).")
        print("This suite is retrieval-heavy; a few full runs exhaust the daily "
              "cap. Wait for")
        print("the quota to reset, or set LLM_PROVIDER=anthropic, then re-run.")
    if failures:
        print()
        print(f"{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    if skipped:
        return 2
    print("All agent checks passed.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", help="Print full answers.")
    parser.add_argument("--only-multiturn", action="store_true",
                        help="Skip the tool-selection scenarios (saves LLM quota).")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.verbose, args.only_multiturn)))
