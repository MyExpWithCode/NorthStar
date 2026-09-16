"""The source registry -- `data/kb/sources.json`.

This file, not the directory listing, is the authority on what is in the
knowledge base. Keeping it authoritative is what lets the `/admin` UI show
licences and chunk counts, lets a removal delete a document, its registry entry
and its chunks together, and lets the assistant answer **which destinations it
actually covers** rather than guessing.

Three fields carry meaning beyond bookkeeping:

* `destination` -- the place a document is about. Retrieval filters on it, so a
  question about a city with no documents is refused instead of being answered
  from another city's guide.
* `kind` -- what sort of document it is. The chunker uses this instead of
  parsing source ids, so adding a new destination needs no code change.
* `facets` -- known subjects of the whole document, for documents whose lead
  section carries no heading to classify.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings

#: How the source entered the knowledge base.
#:   curated -- shipped with the project, seeded by `app.ingest.fetch_sources`
#:   url     -- added at runtime through the /admin UI by URL
#:   upload  -- added at runtime through the /admin UI by file upload
Origin = Literal["curated", "url", "upload"]

#: `unavailable` records a source we deliberately tried and could not use.
#: Keeping the failed attempt visible is more honest than dropping it.
State = Literal["available", "unavailable", "missing"]

#: What kind of document this is. Drives chunk tagging without the chunker
#: having to recognise source-id patterns.
#:   guide     -- a destination's main travel guide
#:   district  -- a neighbourhood or district guide
#:   itinerary -- a day-by-day plan
#:   reference -- an encyclopaedic article on one subject
#:   user      -- added by a user through /admin
Kind = Literal["guide", "district", "itinerary", "reference", "user"]

REGISTRY_VERSION = 2

#: Used when a document is not about one specific place.
UNKNOWN_DESTINATION = ""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalise_destination(name: str) -> str:
    """Canonical comparison form for a destination name.

    Matching is case- and whitespace-insensitive so that "singapore",
    "Singapore" and " Singapore " are the same place.
    """
    return " ".join((name or "").split()).casefold()


class SourceRecord(BaseModel):
    source_id: str
    source_title: str
    source_url: str
    publisher: str
    license: str
    origin: Origin = "curated"
    #: The place this document is about. Empty when it is not place-specific.
    destination: str = UNKNOWN_DESTINATION
    kind: Kind = "reference"
    #: Subjects of the document as a whole, for chunks whose own heading says
    #: nothing (typically a document's lead section).
    facets: list[str] = Field(default_factory=list)
    #: Project-root-relative POSIX path, so the registry stays portable.
    doc_path: str | None = None
    retrieved_at: str | None = None
    #: True when the document itself is committed to git (permissive licence).
    committed: bool = False
    chunk_count: int = 0
    state: State = "available"
    #: Human-readable reason for a non-available state.
    note: str | None = None

    @property
    def resolved_path(self) -> Path | None:
        if self.doc_path is None:
            return None
        return settings.project_root / self.doc_path

    def document_exists(self) -> bool:
        path = self.resolved_path
        return path is not None and path.is_file()


class DestinationSummary(BaseModel):
    """What the knowledge base holds for one place."""

    destination: str
    document_count: int
    chunk_count: int
    kinds: list[str] = Field(default_factory=list)


class SourceRegistry(BaseModel):
    version: int = REGISTRY_VERSION
    updated_at: str = Field(default_factory=utc_now_iso)
    sources: list[SourceRecord] = Field(default_factory=list)

    # -- persistence --------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> SourceRegistry:
        """Load the registry, returning an empty one if it does not exist yet."""
        path = path or settings.registry_path
        if not path.is_file():
            return cls()
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path | None = None) -> Path:
        """Write atomically so an interrupted save cannot truncate the registry."""
        path = path or settings.registry_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = utc_now_iso()
        self.version = REGISTRY_VERSION
        self.sources.sort(key=lambda record: (record.destination, record.source_id))
        payload = self.model_dump_json(indent=2) + "\n"
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, path)
        return path

    # -- queries ------------------------------------------------------------
    def get(self, source_id: str) -> SourceRecord | None:
        return next((r for r in self.sources if r.source_id == source_id), None)

    def available(self) -> list[SourceRecord]:
        """Sources with a document on disk -- the ones the chunker should read."""
        return [
            r for r in self.sources if r.state == "available" and r.document_exists()
        ]

    def total_chunks(self) -> int:
        return sum(r.chunk_count for r in self.sources)

    def destinations(self) -> list[DestinationSummary]:
        """Places the knowledge base can actually answer about, best-covered first.

        Only `available` sources count. A destination whose documents are all
        missing or unavailable is not covered, and saying otherwise would be
        the kind of unsupported claim this application exists to avoid.
        """
        grouped: dict[str, list[SourceRecord]] = defaultdict(list)
        for record in self.available():
            if record.destination:
                grouped[record.destination].append(record)
        summaries = [
            DestinationSummary(
                destination=name,
                document_count=len(records),
                chunk_count=sum(r.chunk_count for r in records),
                kinds=sorted({r.kind for r in records}),
            )
            for name, records in grouped.items()
        ]
        return sorted(
            summaries, key=lambda s: (-s.document_count, s.destination)
        )

    def destination_names(self) -> list[str]:
        return [summary.destination for summary in self.destinations()]

    def resolve_destination(self, name: str) -> str | None:
        """Map a user- or model-supplied name to a covered destination.

        Returns None when the place is not covered, which the knowledge-base
        tool turns into an explicit "I do not have a knowledge base for that"
        rather than results from a different city.
        """
        wanted = normalise_destination(name)
        if not wanted:
            return None
        covered = self.destination_names()
        for candidate in covered:
            if normalise_destination(candidate) == wanted:
                return candidate
        # Tolerate "Singapore City" / "city of Singapore" style variations
        # without matching unrelated places.
        for candidate in covered:
            normalised = normalise_destination(candidate)
            if normalised in wanted or wanted in normalised:
                return candidate
        return None

    # -- mutation -----------------------------------------------------------
    def upsert(self, record: SourceRecord) -> SourceRecord:
        """Insert or replace a record, preserving the existing chunk count.

        Chunk counts are owned by the index builder, so a re-fetch must not
        reset them to zero and make the registry disagree with the index.
        """
        existing = self.get(record.source_id)
        if existing is not None:
            if record.chunk_count == 0:
                record.chunk_count = existing.chunk_count
            self.sources.remove(existing)
        self.sources.append(record)
        return record

    def remove(
        self, source_id: str, delete_document: bool = True
    ) -> SourceRecord | None:
        """Remove a source and, by default, its document."""
        record = self.get(source_id)
        if record is None:
            return None
        if delete_document:
            path = record.resolved_path
            if path is not None and path.is_file():
                path.unlink()
        self.sources.remove(record)
        return record

    def remove_destination(self, destination: str) -> list[SourceRecord]:
        """Remove every document for one place."""
        resolved = self.resolve_destination(destination) or destination
        doomed = [
            r for r in self.sources
            if normalise_destination(r.destination) == normalise_destination(resolved)
        ]
        for record in doomed:
            self.remove(record.source_id, delete_document=True)
        return doomed

    def unassigned(self) -> list[SourceRecord]:
        """Available documents with no destination.

        These are a trap rather than a curiosity: a destination-scoped search
        can never return them, so a travel document left unassigned is indexed
        but unreachable. The /admin UI surfaces them for exactly that reason.
        """
        return [r for r in self.available() if not r.destination]

    def update_source(
        self,
        source_id: str,
        *,
        destination: str | None = None,
        title: str | None = None,
    ) -> SourceRecord:
        """Correct an existing source's destination and/or title.

        Both are fixable after the fact for good reason: the destination is
        what makes a document reachable by a scoped search, and the title is
        what appears in every citation. A URL import guesses the title from the
        first heading, which on Wikipedia is often the navigation "Contents".

        The frontmatter is rewritten alongside the registry entry, so the
        document stays self-describing and a re-read cannot disagree.
        """
        record = self.get(source_id)
        if record is None:
            raise KeyError(source_id)

        if destination is not None:
            record.destination = " ".join(destination.split())
        if title is not None and title.strip():
            record.source_title = " ".join(title.split())

        path = record.resolved_path
        if path is not None and path.is_file():
            from app.ingest import frontmatter

            metadata, body = frontmatter.loads(path.read_text(encoding="utf-8"))
            if metadata:
                metadata["destination"] = record.destination
                metadata["source_title"] = record.source_title
                path.write_text(
                    frontmatter.dumps(metadata, body), encoding="utf-8"
                )
        return record

    def set_destination(self, source_id: str, destination: str) -> SourceRecord:
        """Back-compatible shim for the destination-only case."""
        return self.update_source(source_id, destination=destination)

    def set_chunk_counts(self, counts: dict[str, int]) -> None:
        for record in self.sources:
            record.chunk_count = counts.get(record.source_id, 0)

    def reconcile(self) -> list[str]:
        """Flag registered sources whose document has gone missing.

        This happens legitimately on a fresh clone: `sources.json` is committed
        but the documents we may not redistribute are not. Marking them
        `missing` keeps the attempt visible and keeps the chunker from failing.
        """
        drifted: list[str] = []
        for record in self.sources:
            if record.state == "unavailable":
                continue
            if record.document_exists():
                if record.state == "missing":
                    record.state = "available"
            else:
                record.state = "missing"
                record.chunk_count = 0
                drifted.append(record.source_id)
        return drifted


#: Guards the load-modify-save sequence.
#:
#: Every writer previously did its own load, edited the object and saved, which
#: is a lost-update race: the background rebuild writes chunk counts while an
#: HTTP request writes a destination, and whichever saves last silently
#: discards the other edit. Observed in practice. A single-process app only
#: needs an in-process lock; a multi-process deployment would want the data in
#: a store that does its own transactions.
_write_lock = threading.RLock()


@contextmanager
def transaction():
    """Load the registry, let the caller edit it, then save -- atomically.

    Reentrant, so a helper that itself opens a transaction can be called from
    inside one without deadlocking.
    """
    with _write_lock:
        registry = SourceRegistry.load()
        yield registry
        registry.save()


def relative_doc_path(path: Path) -> str:
    """Project-root-relative POSIX path for storage in the registry."""
    return path.resolve().relative_to(settings.project_root).as_posix()
