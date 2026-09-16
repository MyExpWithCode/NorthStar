"""Exercise retrieval against the brief's own questions.

    python scripts/smoke_rag.py

Checks that every destination question in section 4.1 of the brief retrieves
grounded content with citable sources, that out-of-scope questions are refused
rather than answered, and that the category filter works.
"""

from __future__ import annotations

import sys as _sys

# The assistant's provenance labels are emoji; a Windows cp1252 console would
# raise UnicodeEncodeError when printing them.
for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import sys

from app.rag import retriever
from app.rag.kb_tool import search_travel_knowledge_base
from app.rag.retriever import NO_RELEVANT_CONTENT

BRIEF_QUESTIONS = [
    "What are the must-visit attractions in Singapore?",
    "Which neighbourhoods are suitable for cultural experiences?",
    "How can a tourist travel around Singapore?",
    "Suggest activities for a family with children.",
    "Create a three-day sightseeing itinerary.",
    "What indoor attractions can I visit?",
]

OUT_OF_SCOPE = [
    "What are the best ski resorts in Singapore?",
    "Explain the Riemann hypothesis.",
    "What is the capital of Peru?",
]


def invoke(
    query: str,
    categories: list[str] | None = None,
    k: int | None = None,
    destination: str = "",
):
    """Call the tool the way the agent will, returning (content, artifact).

    A tool declared `content_and_artifact` only yields its artifact when it is
    invoked with a tool-call-shaped payload; a plain `.invoke({...args})`
    returns the content string alone. The agent always calls it the first way,
    so the smoke test must too.
    """
    message = search_travel_knowledge_base.invoke(
        {
            "name": "search_travel_knowledge_base",
            # The tool takes [] / 0 rather than None: a nullable schema made
            # Groq reject whole tool calls server-side (see kb_tool.py).
            "args": {
                "query": query,
                "destination": destination,
                "categories": categories or [],
                "k": k or 0,
            },
            "id": "smoke-test",
            "type": "tool_call",
        }
    )
    return message.content, (message.artifact or {})


def main() -> int:
    if not retriever.is_ready():
        print("No index. Run `python -m app.ingest.build_index` first.")
        return 1

    failures: list[str] = []

    print("=" * 78)
    print("BRIEF SECTION 4.1 QUESTIONS -- must retrieve grounded, citable content")
    print("=" * 78)
    for question in BRIEF_QUESTIONS:
        content, artifact = invoke(question)
        sources = artifact.get("sources", [])
        grounded = bool(sources) and NO_RELEVANT_CONTENT not in content
        print(f"\n{'OK ' if grounded else 'FAIL'} {question}")
        if not grounded:
            failures.append(f"no grounded content for: {question}")
            continue
        for source in sources[:3]:
            print(f"       [{source['marker']}] {source['score']:.2f}  "
                  f"{source['section_path']}")
        print(f"       sources: {len(sources)} | "
              f"distinct documents: {len({s['chunk_id'].split('#')[0] for s in sources})}")

    print()
    print("=" * 78)
    print("OUT-OF-SCOPE QUESTIONS -- must be refused, not answered")
    print("=" * 78)
    for question in OUT_OF_SCOPE:
        content, artifact = invoke(question)
        refused = NO_RELEVANT_CONTENT in content
        marker = "OK " if refused else "note"
        print(f"\n{marker} {question}")
        if refused:
            print("       retrieval floor refused it (no chunks above threshold)")
        else:
            top = artifact["sources"][0]
            print(f"       passed the floor at {top['score']:.2f} -> "
                  f"{top['section_path']}")
            print("       the prompt layer must refuse this one "
                  "(see docs/ARCHITECTURE.md 6.1)")

    print()
    print("=" * 78)
    print("CATEGORY FILTER -- the mechanism behind wet-weather alternatives")
    print("=" * 78)
    for categories in (["indoor"], ["outdoor"], ["itinerary"], ["transport"]):
        _, artifact = invoke("things to do in Singapore", categories=categories)
        sources = artifact.get("sources", [])
        ok = bool(sources) and all(
            categories[0] in s["categories"] for s in sources
        )
        print(f"\n{'OK ' if ok else 'FAIL'} categories={categories}: "
              f"{len(sources)} sources, all correctly tagged: {ok}")
        if not ok:
            failures.append(f"category filter leaked for {categories}")
        for source in sources[:2]:
            print(f"       {source['score']:.2f}  {source['section_path']}")

    print()
    print("=" * 78)
    print("DESTINATION SCOPING -- the knowledge base is per place")
    print("=" * 78)
    covered = retriever.destination_names()
    print(f"\ncovered destinations: {covered}")

    for place in covered:
        _, artifact = invoke("must-visit attractions", destination=place)
        places = {s["destination"] for s in artifact.get("sources", [])}
        ok = bool(places) and places == {place}
        print(f"\n{'OK ' if ok else 'FAIL'} scoped to {place}: "
              f"{len(artifact.get('sources', []))} sources, "
              f"destinations={places or 'none'}")
        if not ok:
            failures.append(f"scoping to {place} returned {places}")

    for unknown in ("Reykjavik", "Rome", "Ulaanbaatar"):
        content, artifact = invoke("must-visit attractions", destination=unknown)
        refused = content.startswith("DESTINATION_NOT_COVERED")
        no_sources = not artifact.get("sources")
        names_covered = bool(artifact.get("covered_destinations"))
        ok = refused and no_sources and names_covered
        print(f"\n{'OK ' if ok else 'FAIL'} uncovered {unknown}: "
              f"refused={refused}, "
              f"sources={len(artifact.get('sources', []))}, "
              f"named what it covers={names_covered}")
        if not ok:
            failures.append(f"{unknown} was not refused cleanly")

    for variant in ("singapore", "  SINGAPORE  "):
        _, artifact = invoke("hawker food", destination=variant)
        resolved = artifact.get("destination")
        ok = resolved == "Singapore"
        print(f"\n{'OK ' if ok else 'FAIL'} {variant!r} resolved to {resolved!r}")
        if not ok:
            failures.append(f"{variant!r} did not resolve to Singapore")

    print()
    print("=" * 78)
    print("UNAVAILABLE KNOWLEDGE BASE -- must not fabricate")
    print("=" * 78)
    original = retriever.is_ready
    retriever.is_ready = lambda: False
    try:
        content, artifact = invoke("What are the must-visit attractions?")
        handled = "KNOWLEDGE_BASE_UNAVAILABLE" in content
        print(f"\n{'OK ' if handled else 'FAIL'} index missing -> "
              f"{content.splitlines()[0]}")
        print(f"       artifact error: {artifact.get('error')}")
        if not handled:
            failures.append("missing index was not reported")
    finally:
        retriever.is_ready = original

    print()
    print("=" * 78)
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All retrieval checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
