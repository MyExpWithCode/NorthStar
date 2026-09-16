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

#: Returned instead of weak chunks when nothing clears the relevance floor.
#: The prompt turns this into a plain statement that the knowledge base does
#: not cover the question, rather than an invented answer.
NO_RELEVANT_CONTENT = "NO_RELEVANT_CONTENT"

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


def status() -> dict:
    """Index health, for GET /health and the /admin status header."""
    manifest = read_manifest()
    return {
        "ready": index_exists() and manifest is not None,
        "loaded": _store is not None,
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
    categories: list[str] | None = None,
    k: int | None = None,
) -> list[Hit]:
    """Retrieve chunks above the relevance floor, best first.

    `categories` filters on the tags assigned at ingest -- passing
    `["indoor"]` is how the agent finds wet-weather alternatives.

    Over-fetching then filtering (rather than filtering inside FAISS) keeps the
    scores comparable between a filtered and an unfiltered search, and avoids
    depending on vector-store-specific filter semantics.
    """
    k = k or settings.retrieval_k
    wanted = {c.strip().lower() for c in (categories or []) if c.strip()}
    fetch_k = k * 6 if wanted else k

    results = get_store().similarity_search_with_relevance_scores(query, k=fetch_k)

    hits: list[Hit] = []
    for document, score in results:
        if score < settings.relevance_floor:
            continue
        if wanted and not wanted <= {
            c.lower() for c in document.metadata.get("categories", [])
        }:
            continue
        hits.append(Hit(document=document, score=float(score)))
        if len(hits) == k:
            break
    return hits
