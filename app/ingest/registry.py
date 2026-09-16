"""The source registry -- `data/kb/sources.json`.

This file, not the directory listing, is the authority on what is in the
knowledge base. Keeping it authoritative is what lets the `/admin` UI show
licences and chunk counts, and lets a removal delete a document, its registry
entry and its chunks together instead of leaving a half-deleted state.
"""

from __future__ import annotations

import json
import os
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

REGISTRY_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SourceRecord(BaseModel):
    source_id: str
    source_title: str
    source_url: str
    publisher: str
    license: str
    origin: Origin = "curated"
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
        self.sources.sort(key=lambda record: record.source_id)
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

    def remove(self, source_id: str, delete_document: bool = True) -> SourceRecord | None:
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


def relative_doc_path(path: Path) -> str:
    """Project-root-relative POSIX path for storage in the registry."""
    return path.resolve().relative_to(settings.project_root).as_posix()
