"""Vector search over the knowledge base, with the grounding guard.

The index is held as a process-wide singleton because loading it costs real
time and the embedding model costs more. `reload()` exists so that an index
rebuild triggered from the /admin UI becomes visible to chat without
restarting the process -- and, crucially, chat keeps answering from the
already-loaded index while a rebuild is in flight.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document

from app.config import settings
from app.ingest.build_index import index_exists, load_vector_store, read_manifest
from app.ingest.registry import (
    DestinationSummary,
    SourceRegistry,
    normalise_destination,
)

#: Returned instead of weak chunks when nothing clears the relevance floor.
#: The prompt turns this into a plain statement that the knowledge base does
#: not cover the question, rather than an invented answer.
NO_RELEVANT_CONTENT = "NO_RELEVANT_CONTENT"

#: Returned when the question is about a place the knowledge base has no
#: documents for. Distinct from NO_RELEVANT_CONTENT on purpose: "I have no
#: guide for Rome" and "my Singapore guide does not mention that" are
#: different admissions, and conflating them would let a Rome question be
#: answered from Singapore content.
DESTINATION_NOT_COVERED = "DESTINATION_NOT_COVERED"

#: Hard ceiling on excerpts per search, regardless of what the model asks for.
#: Each excerpt is up to ~1 kB of text, so an unbounded k lets a single tool
#: call blow the context window -- and on a metered provider, the
#: tokens-per-minute budget. Observed: the model asking for 15 excerpts across
#: three searches in one turn triggered HTTP 429/413 from Groq.
MAX_RETRIEVAL_K = 5

_store: FAISS | None = None
_lock = threading.Lock()


def get_store() -> FAISS:
    """Load the index once and reuse it."""
    global _store
    with _lock:
        if _store is None:
            _store = load_vector_store()
        return _store


def reload() -> None:
    """Drop the cached index so the next search picks up a rebuilt one."""
    global _store
    with _lock:
        _store = None


def is_ready() -> bool:
    return index_exists()


def destinations() -> list[DestinationSummary]:
    """Places the knowledge base can answer about, best-covered first."""
    registry = SourceRegistry.load()
    registry.reconcile()
    return registry.destinations()


def destination_names() -> list[str]:
    return [summary.destination for summary in destinations()]


def resolve_destination(name: str) -> str | None:
    """Map a requested place onto a covered one, or None if not covered."""
    registry = SourceRegistry.load()
    registry.reconcile()
    return registry.resolve_destination(name)


def status() -> dict:
    """Index health, for GET /health and the /admin status header."""
    manifest = read_manifest()
    return {
        "ready": index_exists() and manifest is not None,
        "loaded": _store is not None,
        "destinations": [d.model_dump() for d in destinations()],
        "relevance_floor": settings.relevance_floor,
        "retrieval_k": settings.retrieval_k,
        "manifest": manifest,
    }


@dataclass(frozen=True)
class Hit:
    """One retrieved chunk with its cosine similarity to the query."""

    document: Document
    score: float

    @property
    def metadata(self) -> dict:
        return self.document.metadata

    def as_source(self, marker: str) -> dict:
        """Citation payload for the API and the UI."""
        return {
            "marker": marker,
            "title": self.metadata["source_title"],
            "destination": self.metadata.get("destination", ""),
            "url": self.metadata["source_url"],
            "section_path": self.metadata["section_path"],
            "publisher": self.metadata["publisher"],
            "license": self.metadata["license"],
            "chunk_id": self.metadata["chunk_id"],
            "categories": self.metadata["categories"],
            "score": round(self.score, 3),
        }


def search(
    query: str,
    destination: str | None = None,
    categories: list[str] | None = None,
    k: int | None = None,
) -> list[Hit]:
    """Retrieve chunks above the relevance floor, best first.

    `destination` restricts results to one place. It matters because the
    knowledge base can hold several: without it, a question about Tokyo could
    be answered from Singapore's guide, which is exactly the kind of
    confident-but-wrong answer this application is built to avoid.

    `categories` filters on the tags assigned at ingest -- passing
    `["indoor"]` is how the agent finds wet-weather alternatives.

    The filter is **disjunctive**: a chunk matching ANY requested tag is kept.
    Requiring all of them looked tidier but was destructive in practice --
    asked for family activities the model passed
    `["attractions", "indoor", "outdoor", "food"]`, and since few chunks carry
    all four the search returned nothing at all. Tags are assigned by rules at
    ingest, so treating a list as "any of these facets" is both what a model
    means by it and what actually retrieves content.

    Over-fetching then filtering (rather than filtering inside FAISS) keeps the
    scores comparable between a filtered and an unfiltered search, and avoids
    depending on vector-store-specific filter semantics.
    """
    k = min(k or settings.retrieval_k, MAX_RETRIEVAL_K)
    wanted = {c.strip().lower() for c in (categories or []) if c.strip()}
    place = normalise_destination(destination or "")
    fetch_k = k * 6 if (wanted or place) else k

    results = get_store().similarity_search_with_relevance_scores(query, k=fetch_k)

    hits: list[Hit] = []
    for document, score in results:
        if score < settings.relevance_floor:
            continue
        if place and normalise_destination(
            document.metadata.get("destination", "")
        ) != place:
            continue
        if wanted and wanted.isdisjoint(
            {c.lower() for c in document.metadata.get("categories", [])}
        ):
            continue
        hits.append(Hit(document=document, score=float(score)))
        if len(hits) == k:
            break
    return hits
