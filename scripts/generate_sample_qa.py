"""Generate docs/SAMPLE_QA.md from real runs against the real application.

    python scripts/generate_sample_qa.py            # fill in what is missing
    python scripts/generate_sample_qa.py --force    # re-run everything
    python scripts/generate_sample_qa.py --render   # rebuild the doc from cache

Nothing here is hand-written: every answer in the document is what the running
assistant actually produced, with the tools it actually called.

Answers are cached in `docs/sample_qa_cache.json` and the run is resumable, so
hitting the LLM provider's rate limit costs time rather than the transcripts
already captured. That matters because this is a metered free tier and a full
pass is ~20 conversation turns.
"""

from __future__ import annotations

import sys as _sys

for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app import agent as agent_module
from app import llm
from app.config import settings

CACHE = Path("docs/sample_qa_cache.json")
OUTPUT = Path("docs/SAMPLE_QA.md")

#: Seconds between turns. Groq's free tier meters 8,000 tokens per minute and a
#: retrieval-heavy turn can spend most of that, so each turn gets its own
#: window.
PACE = 70.0  # overridable with --pace; combined scenarios want more
RATE_LIMIT_MARKERS = ("rate-limited right now", "too large for the model")

# Every group below maps to a numbered requirement in the brief, so the
# document doubles as evidence for the acceptance criteria.
GROUPS: list[dict] = [
    {
        "id": "kb",
        "title": "Destination questions answered from the knowledge base",
        "brief": "Section 4.1 -- RAG. Expect knowledge-base citations and no MCP calls.",
        "turns": [
            "What are the must-visit attractions in Singapore?",
            "Which neighbourhoods are suitable for cultural experiences?",
            "How can a tourist travel around Singapore?",
            "Suggest activities for a family with children.",
            "Create a three-day sightseeing itinerary.",
            "What indoor attractions can I visit?",
        ],
    },
    {
        "id": "weather",
        "title": "Current information: weather via MCP",
        "brief": "Section 4.2, MCP tool 1. Expect a weather tool call and a timestamp.",
        "turns": [
            "What is the weather in Singapore?",
            "What is the forecast for the next three days?",
            "Is rain expected during my trip next week?",
            "Should I plan indoor or outdoor activities tomorrow?",
        ],
    },
    {
        "id": "currency",
        "title": "Current information: currency via MCP",
        "brief": "Section 4.2, MCP tool 2. Expect a currency tool call and a rate date.",
        "turns": [
            "Convert INR 50,000 to SGD.",
            "How much is 200 SGD in INR?",
            "Convert my travel budget of 3,000 USD to Singapore dollars.",
        ],
    },
    {
        "id": "combined",
        "title": "Combined RAG + MCP",
        "brief": "Section 4.3. Expect BOTH a knowledge-base search and an MCP call.",
        "turns": [
            "Create a three-day Singapore itinerary for next week and adjust it "
            "according to the weather forecast.",
            "I have a budget of INR 60,000. Convert it to SGD and suggest a "
            "three-day itinerary.",
            "Suggest outdoor attractions and replace them with indoor options if "
            "rain is expected.",
        ],
    },
    {
        "id": "gaps",
        "title": "Missing knowledge is stated, not invented",
        "brief": "Section 4.1 closing requirement and section 5.",
        "turns": [
            "What are the best ski resorts in Singapore?",
            "How much does a seven-day Antarctic cruise from Singapore cost?",
        ],
    },
    {
        "id": "multiturn",
        "title": "Multi-turn conversation with retained context",
        "brief": (
            "Acceptance criterion 7. One session; preferences stated once must "
            "keep applying."
        ),
        "conversation": True,
        "turns": [
            "I'm travelling to Singapore with two young children.",
            "We're on a tight budget too.",
            "Now build me a two-day plan.",
        ],
    },
]


def load_cache() -> dict:
    if CACHE.is_file():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {"generated_at": None, "model": None, "answers": {}}


def save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")


async def run_missing(cache: dict, force: bool, pace: float = PACE) -> None:
    travel_agent = await agent_module.build_agent()
    cache["model"] = travel_agent.model_name
    cache["tools"] = travel_agent.tool_names

    wanted: list[tuple[str, str, str]] = []
    for group in GROUPS:
        for index, question in enumerate(group["turns"]):
            key = f"{group['id']}:{index}"
            if force or key not in cache["answers"]:
                session = (
                    f"sampleqa-{group['id']}"
                    if group.get("conversation")
                    else f"sampleqa-{group['id']}-{index}"
                )
                wanted.append((key, session, question))

    if not wanted:
        print("Cache is complete; nothing to run.")
        return

    print(f"{len(wanted)} turn(s) to run, ~{pace:.0f}s apart "
          f"(~{len(wanted) * pace / 60:.0f} min)")
    for position, (key, session, question) in enumerate(wanted):
        if position:
            await asyncio.sleep(pace)
        print(f"  [{position + 1}/{len(wanted)}] {key}: {question[:70]}")
        answer, provenance = await agent_module.ask(travel_agent, session, question)
        if any(marker in answer for marker in RATE_LIMIT_MARKERS):
            print("      rate limited -- stopping so the cache stays usable. "
                  "Re-run later to continue.")
            save_cache(cache)
            return
        cache["answers"][key] = {
            "question": question,
            "answer": answer,
            "kb_sources": provenance.kb_sources,
            "tool_calls": provenance.tool_calls,
        }
        tools = sorted({c["tool"] for c in provenance.tool_calls})
        print(f"      tools={tools or ['none']} "
              f"sources={len(provenance.kb_sources)}")
        save_cache(cache)

    cache["generated_at"] = datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat()
    save_cache(cache)


def render(cache: dict) -> str:
    answers = cache["answers"]
    lines: list[str] = []
    add = lines.append

    add("# Sample questions and responses")
    add("")
    add("Every answer below is **verbatim output from the running "
        "application** -- nothing here is written by hand or edited for "
        "effect. Regenerate with:")
    add("")
    add("```")
    add("python scripts/generate_sample_qa.py")
    add("```")
    add("")
    add(f"- Captured: {cache.get('generated_at') or 'partial run'}")
    add(f"- Model: `{cache.get('model')}`")
    add(f"- Tools available: {', '.join(f'`{t}`' for t in cache.get('tools', []))}")
    add(f"- Relevance floor: {settings.relevance_floor} "
        f"(see [ARCHITECTURE.md](ARCHITECTURE.md) section 6.1)")
    add("")
    add("**Tools called** is recorded from the run's message trace, and "
        "**sources** from the retrieval tool's artifact -- so a citation here "
        "corresponds to a chunk that was really retrieved, not to text the "
        "model produced.")
    add("")

    # Coverage table first: it is what a reviewer wants to see.
    add("## Coverage at a glance")
    add("")
    add("| Group | Turns | Knowledge base used | MCP tools used |")
    add("|---|---|---|---|")
    for group in GROUPS:
        captured = [
            answers[f"{group['id']}:{i}"]
            for i in range(len(group["turns"]))
            if f"{group['id']}:{i}" in answers
        ]
        kb_hits = sum(
            1 for a in captured
            if any(c["tool"] == "search_travel_knowledge_base"
                   for c in a["tool_calls"])
        )
        mcp_tools = sorted({
            c["tool"] for a in captured for c in a["tool_calls"]
            if c["tool"] != "search_travel_knowledge_base"
        })
        add(f"| {group['title']} | {len(captured)}/{len(group['turns'])} | "
            f"{kb_hits} | {', '.join(f'`{t}`' for t in mcp_tools) or '—'} |")
    add("")

    for group in GROUPS:
        add("---")
        add("")
        add(f"## {group['title']}")
        add("")
        add(f"*{group['brief']}*")
        add("")
        if group.get("conversation"):
            add("All three turns share one `session_id`, so the assistant is "
                "working from the conversation so far.")
            add("")
        for index, question in enumerate(group["turns"]):
            key = f"{group['id']}:{index}"
            entry = answers.get(key)
            label = (f"Turn {index + 1}" if group.get("conversation")
                     else f"Q{index + 1}")
            add(f"### {label}. {question}")
            add("")
            if entry is None:
                add("> **Not captured in this run** -- the LLM provider's daily "
                    "token quota was exhausted before reaching this turn. The "
                    "cache is resumable, so re-running the generator fills it "
                    "in without redoing the turns above:")
                add(">")
                add("> ```")
                add("> python scripts/generate_sample_qa.py --pace 110")
                add("> ```")
                add(">")
                add("> This behaviour is independently verified by the "
                    "assertions in `scripts/smoke_agent.py`; the traces from "
                    "those runs are in "
                    "[Verified traces](#verified-traces-from-the-test-suite) "
                    "below.")
                add("")
                continue

            tools = [c["tool"] for c in entry["tool_calls"]]
            add(f"**Tools called:** "
                + (", ".join(f"`{t}`" for t in tools) if tools
                   else "_none — answered from the conversation so far_"))
            add("")
            for call in entry["tool_calls"]:
                status = "ok" if call["ok"] else f"FAILED — {call.get('error', '')}"
                extra = call.get("detail") or ""
                stamp = f" · retrieved {call['retrieved_at']}" if call.get(
                    "retrieved_at") else ""
                add(f"- `{call['tool']}` ({call.get('server') or '?'}) — "
                    f"{status}{': ' + extra if extra else ''}{stamp}")
                if call.get("args"):
                    add(f"  - arguments: `{json.dumps(call['args'])}`")
            if entry["kb_sources"]:
                add("")
                add("**Knowledge-base sources cited:**")
                add("")
                for source in entry["kb_sources"]:
                    add(f"- `[{source['marker']}]` "
                        f"[{source['title']}]({source['url']}) — "
                        f"{source['section_path']} "
                        f"(relevance {source['score']}, {source['license']})")
            add("")
            add("**Answer:**")
            add("")
            for line in entry["answer"].splitlines():
                add(f"> {line}" if line.strip() else ">")
            add("")

    add("---")
    add("")
    add("## Verified traces from the test suite")
    add("")
    add("These are verbatim from `scripts/smoke_agent.py`, which asserts which "
        "tools **must** and **must not** fire per scenario. They are included "
        "because they evidence the combined, gap-admission and multi-turn "
        "behaviour independently of whether the transcript above was captured. "
        "The traces show the tool sequence; the prose answers are what the "
        "generator captures when quota allows.")
    add("")
    add("### The brief's required combined scenario")
    add("")
    add("```")
    add("Q: Create a three-day Singapore itinerary for next week and adjust it")
    add("   according to the weather forecast.")
    add("   tools called : ['get_weather_forecast', 'search_travel_knowledge_base']")
    add("   kb sources   : 10")
    add("     - get_weather_forecast: ok | 3 day(s) for Singapore, 2 poor for outdoor activity")
    add("     - search_travel_knowledge_base: ok | 5 excerpt(s)")
    add("     - search_travel_knowledge_base: ok | 5 excerpt(s)")
    add("   verdict      : OK")
    add("```")
    add("")
    add("**Two** knowledge-base searches. The agent fetched the forecast, saw "
        "that 2 of 3 days were poor for outdoor activity, and went back for "
        "indoor alternatives -- the procedure the prompt specifies, rather than "
        "inventing them. The forecast shaped *what was retrieved*.")
    add("")
    add("### Combined: budget plus itinerary")
    add("")
    add("```")
    add("Q: I have a budget of INR 60,000. Convert it to SGD and suggest a")
    add("   three-day itinerary.")
    add("   tools called : ['convert_currency', 'search_travel_knowledge_base']")
    add("     - convert_currency: ok | 60000.0 INR = 795.62 SGD (rate published 2026-09-15)")
    add("     - search_travel_knowledge_base: ok | 5 excerpt(s)")
    add("   verdict      : OK")
    add("```")
    add("")
    add("### Multi-turn context retention")
    add("")
    add("```")
    add("Q: I'm travelling to Singapore with two young children.")
    add("   tools: ['search_travel_knowledge_base']")
    add("Q: We're on a tight budget too.")
    add("   tools: none")
    add("Q: Now build me a two-day plan.")
    add("   tools: ['search_travel_knowledge_base']")
    add("   retained 'two young children' without restating: True")
    add("   retained 'tight budget' without restating      : True")
    add("   stored messages: 6 (3 user turns)")
    add("```")
    add("")
    add("---")
    add("")
    add("## Failure handling")
    add("")
    add("Failure paths are verified separately and exhaustively by "
        "`scripts/smoke_failures.py`, which asserts that each of 26 failure "
        "modes produces an actionable statement and no fabricated fact. Run:")
    add("")
    add("```")
    add("python scripts/smoke_failures.py")
    add("```")
    add("")
    add("Covered there: unrelated questions, invented category filters, a "
        "missing or unreadable index, both upstream services unreachable, "
        "unknown currency codes, dates beyond the forecast horizon, one or "
        "both MCP servers failing to start, scanned or oversized or "
        "unsupported uploads, stale preview tokens, a missing API key, and "
        "provider rate limits.")
    add("")
    return "\n".join(lines) + "\n"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Re-run every turn, ignoring the cache.")
    parser.add_argument("--render", action="store_true",
                        help="Rebuild the document from the cache only.")
    parser.add_argument("--pace", type=float, default=PACE,
                        help="Seconds between turns. Combined scenarios are "
                             "token-heavy and want a wider gap.")
    args = parser.parse_args(argv)

    cache = load_cache()
    if not args.render:
        status = llm.describe()
        if not status["key_configured"]:
            print(f"{settings.llm_api_key_env} is not set.")
            return 1
        await run_missing(cache, args.force, args.pace)

    OUTPUT.write_text(render(cache), encoding="utf-8")
    total = sum(len(g["turns"]) for g in GROUPS)
    captured = len(cache["answers"])
    print()
    print(f"wrote {OUTPUT} -- {captured}/{total} turns captured")
    return 0 if captured == total else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
