"""The knowledge-base search tool exposed to the agent.

This is the *only* route by which destination facts may enter an answer. It is
deliberately a tool rather than a pre-retrieval step, so that the model decides
when destination knowledge is needed -- and, for the flagship scenario, can
come back for `indoor` alternatives after the weather tool reports a wet day.

The tool returns `content_and_artifact`:

* **content** -- numbered excerpts the model cites as `[S1]`, `[S2]`, each
  labelled with its source, section and relevance score. The score is included
  on purpose: a threshold cannot decide whether retrieved text actually answers
  the question (see docs/ARCHITECTURE.md section 6.1), so the model is given
  the evidence to judge that itself.
* **artifact** -- the machine-readable source list the API returns to the UI so
  citations can be rendered as clickable links. It never enters the prompt.
"""

from __future__ import annotations

from typing import Annotated, Literal

from langchain_core.tools import tool

from app.config import settings
from app.rag import retriever
from app.rag.retriever import NO_RELEVANT_CONTENT

#: The tags assigned at ingest. Named explicitly in the schema so the model
#: filters with a real tag instead of inventing one.
Category = Literal[
    "attractions",
    "neighbourhoods",
    "transport",
    "culture",
    "practical",
    "food",
    "itinerary",
    "shopping",
    "accommodation",
    "indoor",
    "outdoor",
]

_NOTHING_FOUND_TEMPLATE = (
    f"{NO_RELEVANT_CONTENT}\n"
    'The knowledge base has nothing above the relevance threshold for: "{query}"'
    "{filter_note}\n"
    "Tell the user this topic is not covered by the travel knowledge base. "
    "Do not answer it from general knowledge."
)


def _format_excerpt(marker: str, hit: retriever.Hit) -> str:
    metadata = hit.metadata
    return (
        f"[{marker}] {metadata['source_title']} — {metadata['section_path']}\n"
        f"relevance {hit.score:.2f} | {metadata['source_url']}\n"
        f"{hit.document.page_content.strip()}"
    )


@tool("search_travel_knowledge_base", response_format="content_and_artifact")
def search_travel_knowledge_base(
    query: str,
    categories: Annotated[
        list[Category] | None,
        "Optional tag filter. Use ['indoor'] for wet-weather alternatives, "
        "['outdoor'] for fair-weather activities, ['itinerary'] for day-by-day "
        "plans, ['transport'] for getting around. Omit to search everything.",
    ] = None,
    k: Annotated[int | None, "How many excerpts to return. Defaults to 6."] = None,
) -> tuple[str, dict]:
    """Search the Singapore travel knowledge base for destination facts.

    Use this for anything about the destination itself: attractions,
    neighbourhoods, getting around, food, culture, practical tips, opening
    hours, prices, and sample itineraries.

    This is the only permitted source of destination facts. Do not answer
    destination questions from your own knowledge.

    Do NOT use this for current information -- weather and exchange rates come
    from the MCP tools instead.

    Returns numbered excerpts `[S1]`, `[S2]`, ... each with its source title,
    section path, relevance score and URL. Cite them by marker. If the result
    begins with NO_RELEVANT_CONTENT, say the knowledge base does not cover the
    topic rather than answering anyway.
    """
    if not retriever.is_ready():
        return (
            "KNOWLEDGE_BASE_UNAVAILABLE\n"
            "The travel knowledge base index has not been built, so no "
            "destination facts can be retrieved. Tell the user the knowledge "
            "base is unavailable; do not answer destination questions from "
            "general knowledge.",
            {"sources": [], "error": "index_not_built"},
        )

    try:
        hits = retriever.search(query, categories=categories, k=k)
    except Exception as exc:  # index corrupt, disk gone, embedding failure
        return (
            "KNOWLEDGE_BASE_ERROR\n"
            f"The knowledge base search failed: {exc}. Tell the user the "
            "knowledge base could not be searched. Do not invent an answer.",
            {"sources": [], "error": str(exc)},
        )

    if not hits:
        filter_note = (
            f" (filtered to categories: {', '.join(categories)})" if categories else ""
        )
        return (
            _NOTHING_FOUND_TEMPLATE.format(query=query, filter_note=filter_note),
            {
                "sources": [],
                "query": query,
                "categories": categories or [],
                "relevance_floor": settings.relevance_floor,
            },
        )

    markers = [f"S{i}" for i in range(1, len(hits) + 1)]
    content = "\n\n".join(
        _format_excerpt(marker, hit) for marker, hit in zip(markers, hits)
    )
    artifact = {
        "sources": [hit.as_source(marker) for marker, hit in zip(markers, hits)],
        "query": query,
        "categories": categories or [],
        "relevance_floor": settings.relevance_floor,
    }
    return content, artifact
