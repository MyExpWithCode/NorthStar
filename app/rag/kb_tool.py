"""The knowledge-base search tool exposed to the agent.

This is the *only* route by which destination facts may enter an answer. It is
deliberately a tool rather than a pre-retrieval step, so the model decides when
destination knowledge is needed -- and, for the flagship scenario, can come
back for `indoor` alternatives after the weather tool reports a wet day.

**The knowledge base is per-destination.** The tool takes the place the question
is about, and if there are no documents for it the tool says so and names the
places it does cover. That distinction is the whole point: answering a question
about Rome out of Singapore's guide would be confidently wrong, which is worse
than admitting the gap. Weather and currency are unaffected -- those are live
tools and work for any city.

The tool returns `content_and_artifact`:

* **content** -- numbered excerpts the model cites as `[S1]`, `[S2]`, each
  labelled with its source, section and relevance score. The score is included
  on purpose: a threshold cannot decide whether retrieved text actually answers
  the question (see docs/ARCHITECTURE.md section 6.1), so the model is given the
  evidence to judge that itself.
* **artifact** -- the machine-readable source list the API returns to the UI so
  citations can be rendered as clickable links. It never enters the prompt.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import tool

from app.config import settings
from app.rag import retriever
from app.rag.retriever import DESTINATION_NOT_COVERED, NO_RELEVANT_CONTENT

#: The tags assigned at ingest.
#:
#: Deliberately NOT expressed as a Literal enum in the tool schema. Groq
#: validates tool arguments server-side and rejects the whole call with a 400
#: when a model invents a value -- observed with `"family"`, which killed the
#: chat turn outright. A permissive `list[str]` that this tool validates itself
#: degrades instead: unknown tags are dropped, the model is told which ones
#: were ignored, and the search still runs.
VALID_CATEGORIES: tuple[str, ...] = (
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
)

#: Characters of chunk text sent to the model per excerpt.
#:
#: Excerpts dominate the request payload, and the payload is re-sent on every
#: call of the agent loop. Groq's free tier allows 8,000 tokens per minute,
#: which an untruncated multi-search turn exceeds. The index keeps the full
#: chunk; this only bounds what travels to the model.
EXCERPT_CHAR_LIMIT = 520


def _covered_list() -> str:
    names = retriever.destination_names()
    if not names:
        return "no destinations at all (the knowledge base is empty)"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _format_excerpt(marker: str, hit: retriever.Hit, show_place: bool) -> str:
    """One citable excerpt, kept as compact as it can usefully be.

    The section path is in the header, and the chunk text starts with the same
    path (prepended at ingest for embedding context), so the duplicate is
    stripped. The source URL is NOT sent to the model: it would cost tokens on
    every call, and the artifact already carries it to the UI, where the
    citation is rendered as a link.
    """
    metadata = hit.metadata
    section = metadata["section_path"]
    body = hit.document.page_content.strip()
    if body.startswith(section):
        body = body[len(section):].strip()
    if len(body) > EXCERPT_CHAR_LIMIT:
        body = body[:EXCERPT_CHAR_LIMIT].rsplit(" ", 1)[0] + " ..."

    # section_path already begins with the document title (it is the H1), so
    # printing the title separately would repeat it on every excerpt.
    label = section if section.startswith(metadata["source_title"]) else (
        metadata["source_title"] + " - " + section
    )
    # Only name the place when the knowledge base holds more than one, so a
    # single-destination deployment pays nothing for the distinction.
    place = (
        " [" + str(metadata.get("destination")) + "]"
        if show_place and metadata.get("destination")
        else ""
    )
    header = (
        "[" + marker + "] " + label + place
        + " (relevance " + format(hit.score, ".2f") + ")"
    )
    return header + chr(10) + body


@tool("search_travel_knowledge_base", response_format="content_and_artifact")
def search_travel_knowledge_base(
    query: str,
    destination: Annotated[
        str,
        "The place the question is about, e.g. 'Singapore'. Always pass this "
        "when the question names or implies a destination. Leave empty only to "
        "search every destination at once.",
    ] = "",
    categories: Annotated[
        list[str],
        "Optional single-tag filter, one of: attractions, neighbourhoods, "
        "transport, culture, practical, food, itinerary, shopping, "
        "accommodation, indoor, outdoor. [] searches everything.",
    ] = [],
    k: Annotated[int, "How many excerpts to return. 0 means the default of 5."] = 0,
) -> tuple[str, dict]:
    """Search the travel knowledge base for destination facts.

    The only permitted source of destination facts: attractions,
    neighbourhoods, transport, food, culture, practical tips, opening hours,
    prices and itineraries. Do not answer destination questions from your own
    knowledge. Not for weather or exchange rates.

    Pass `destination` for the place in question. If the knowledge base has no
    documents for that place, the result starts with DESTINATION_NOT_COVERED and
    lists the places it does cover -- say that plainly instead of answering.

    Returns excerpts marked [S1], [S2] with source, section and relevance
    score; cite them by marker. NO_RELEVANT_CONTENT means the knowledge base
    covers the place but not the topic -- say so rather than answering anyway.
    """
    requested = [c.strip().lower() for c in (categories or []) if c and c.strip()]
    known = [c for c in requested if c in VALID_CATEGORIES]
    unknown = [c for c in requested if c not in VALID_CATEGORIES]
    ignored_note = ""
    if unknown:
        ignored_note = (
            "\n\n(Ignored unknown category filter(s): "
            + ", ".join(unknown)
            + ". Valid values are: "
            + ", ".join(VALID_CATEGORIES)
            + ".)"
        )

    if not retriever.is_ready():
        return (
            "KNOWLEDGE_BASE_UNAVAILABLE\n"
            "The travel knowledge base index has not been built, so no "
            "destination facts can be retrieved. Tell the user the knowledge "
            "base is unavailable; do not answer destination questions from "
            "general knowledge.",
            {"sources": [], "error": "index_not_built"},
        )

    covered = retriever.destination_names()
    resolved: str | None = None
    if destination and destination.strip():
        resolved = retriever.resolve_destination(destination)
        if resolved is None:
            return (
                DESTINATION_NOT_COVERED + "\n"
                "The travel knowledge base has no documents about "
                + repr(destination.strip())
                + ". It covers: " + _covered_list() + ".\n"
                "Tell the user plainly that this destination is not in the "
                "knowledge base, and name what is covered. Do not answer "
                "questions about it from general knowledge. The weather and "
                "currency tools still work for any city, so time-sensitive "
                "questions about it can still be answered.",
                {
                    "sources": [],
                    "requested_destination": destination.strip(),
                    "covered_destinations": covered,
                    "error": "destination_not_covered",
                },
            )

    try:
        hits = retriever.search(
            query, destination=resolved, categories=known, k=k or None
        )
    except Exception as exc:  # index corrupt, disk gone, embedding failure
        return (
            "KNOWLEDGE_BASE_ERROR\n"
            "The knowledge base search failed: " + str(exc) + ". Tell the user "
            "the knowledge base could not be searched. Do not invent an answer.",
            {"sources": [], "error": str(exc)},
        )

    if not hits:
        scope = " about " + resolved if resolved else ""
        filter_note = (
            " (filtered to categories: " + ", ".join(known) + ")" if known else ""
        )
        return (
            NO_RELEVANT_CONTENT + "\n"
            "The knowledge base has nothing" + scope + " above the relevance "
            'threshold for: "' + query + '"' + filter_note + "\n"
            "Tell the user this topic is not covered by the travel knowledge "
            "base. Do not answer it from general knowledge." + ignored_note,
            {
                "sources": [],
                "query": query,
                "destination": resolved or "",
                "covered_destinations": covered,
                "categories": known,
                "ignored_categories": unknown,
                "relevance_floor": settings.relevance_floor,
            },
        )

    show_place = len(covered) > 1
    markers = ["S" + str(i) for i in range(1, len(hits) + 1)]
    content = "\n\n".join(
        _format_excerpt(marker, hit, show_place)
        for marker, hit in zip(markers, hits)
    )
    artifact = {
        "sources": [hit.as_source(marker) for marker, hit in zip(markers, hits)],
        "query": query,
        "destination": resolved or "",
        "covered_destinations": covered,
        "categories": known,
        "ignored_categories": unknown,
        "relevance_floor": settings.relevance_floor,
    }
    return content + ignored_note, artifact
