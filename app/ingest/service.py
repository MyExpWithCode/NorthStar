"""The ingestion service behind the /admin UI.

Four operations, plus a job runner:

* **preview** a URL or an uploaded file -- parse it and show what was extracted
  *without committing anything*. A PDF that extracts as noise is then visible
  before it can reach the index.
* **confirm** a preview -- write the document, register it, and rebuild.
* **remove** a source -- delete document, registry entry and chunks together.
* **rebuild** the index -- chunk, embed, swap.

Rebuilds run as a background job because embedding takes tens of seconds, and
the chat side must stay responsive. Two invariants make that safe:

1. **The working index is never mid-write.** `rebuild_index` builds into a temp
   directory and swaps; a failed rebuild leaves the previous index serving.
2. **One rebuild at a time.** A second request returns the in-flight job rather
   than racing another embedding pass over the same files.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.config import settings
from app.ingest import frontmatter
from app.ingest.build_index import read_manifest, rebuild_index
from app.ingest.parsers import (
    EmptyDocument,
    ParsedDocument,
    UnsupportedDocument,
    parse_to_markdown,
)
from app.ingest.registry import (
    SourceRecord,
    SourceRegistry,
    relative_doc_path,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

JobState = Literal["queued", "running", "succeeded", "failed"]

#: Licence recorded for user-supplied documents. Uploads have no licence we can
#: verify, and leaving the field blank would make a citation look authoritative
#: when it is not.
USER_SUPPLIED_LICENCE = "user-supplied"

#: Previews are held in memory until confirmed or discarded.
MAX_PENDING_PREVIEWS = 20


class IngestionError(RuntimeError):
    """A request that cannot be satisfied, with a message fit for the UI."""


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@dataclass
class Job:
    job_id: str
    kind: str
    state: JobState = "queued"
    stage: str = "queued"
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str | None = None
    log: list[str] = field(default_factory=list)
    error: str | None = None
    result: dict | None = None

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "state": self.state,
            "stage": self.stage,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log": self.log,
            "error": self.error,
            "result": self.result,
        }


@dataclass
class PendingPreview:
    token: str
    parsed: ParsedDocument
    origin: Literal["url", "upload"]
    filename: str
    source_url: str
    created_at: str = field(default_factory=utc_now_iso)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug or "document")[:60]


class IngestionService:
    """Stateful ingestion operations. One instance per application process."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._previews: dict[str, PendingPreview] = {}
        self._lock = threading.Lock()
        self._active_job_id: str | None = None
        #: Called after a successful rebuild so the retriever picks up the new
        #: index. Injected rather than imported to keep ingestion independent
        #: of the retrieval layer.
        self.on_index_rebuilt: Any = None

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        registry = SourceRegistry.load()
        registry.reconcile()
        manifest = read_manifest()
        active = self._jobs.get(self._active_job_id or "")
        return {
            "index": manifest,
            "index_ready": manifest is not None,
            "sources": [record.model_dump() for record in registry.sources],
            "destinations": [d.model_dump() for d in registry.destinations()],
            "source_count": len(registry.available()),
            "total_chunks": registry.total_chunks(),
            "active_job": active.as_dict() if active else None,
            "supported_uploads": sorted(settings.allowed_upload_extensions),
            "max_upload_mb": settings.max_upload_mb,
        }

    def job(self, job_id: str) -> dict:
        job = self._jobs.get(job_id)
        if job is None:
            raise IngestionError(f"No such job: {job_id}")
        return job.as_dict()

    # -- preview -----------------------------------------------------------
    def preview_upload(self, data: bytes, filename: str) -> dict:
        if len(data) > settings.max_upload_bytes:
            raise IngestionError(
                f"{filename} is {len(data) / 1_048_576:.1f} MB, over the "
                f"{settings.max_upload_mb} MB limit."
            )
        parsed = self._parse(data, filename)
        return self._stash(parsed, origin="upload", filename=filename, source_url="")

    def preview_url(self, url: str) -> dict:
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise IngestionError(
                f"{url!r} is not an http(s) URL."
            )
        # Imported here so a URL fetch failure cannot break module import.
        from app.ingest.fetch_sources import _http_get, html_to_markdown

        try:
            html = _http_get(url)
        except Exception as exc:
            raise IngestionError(
                f"Could not fetch {url}: {type(exc).__name__}: {exc}"
            ) from exc

        markdown = html_to_markdown(html)
        filename = _slugify(Path(url.rstrip("/")).name or "page") + ".md"
        parsed = self._parse(markdown.encode("utf-8"), filename)
        return self._stash(parsed, origin="url", filename=filename, source_url=url)

    def _parse(self, data: bytes, filename: str) -> ParsedDocument:
        try:
            return parse_to_markdown(data, filename)
        except (EmptyDocument, UnsupportedDocument) as exc:
            raise IngestionError(str(exc)) from exc

    def _stash(
        self,
        parsed: ParsedDocument,
        *,
        origin: Literal["url", "upload"],
        filename: str,
        source_url: str,
    ) -> dict:
        with self._lock:
            if len(self._previews) >= MAX_PENDING_PREVIEWS:
                oldest = min(self._previews.values(), key=lambda p: p.created_at)
                self._previews.pop(oldest.token, None)
            token = uuid.uuid4().hex[:12]
            self._previews[token] = PendingPreview(
                token=token,
                parsed=parsed,
                origin=origin,
                filename=filename,
                source_url=source_url,
            )
        payload = parsed.as_dict()
        payload.update(
            {
                "preview_token": token,
                "origin": origin,
                "known_destinations": SourceRegistry.load().destination_names(),
                "source_url": source_url,
                "suggested_license": (
                    USER_SUPPLIED_LICENCE if origin == "upload" else "unknown"
                ),
                "text": parsed.preview(),
            }
        )
        return payload

    # -- confirm -----------------------------------------------------------
    def confirm(
        self,
        preview_token: str,
        *,
        title: str | None = None,
        destination: str | None = None,
        license_: str | None = None,
        publisher: str | None = None,
        source_url: str | None = None,
        rebuild: bool = True,
    ) -> dict:
        with self._lock:
            preview = self._previews.pop(preview_token, None)
        if preview is None:
            raise IngestionError(
                "That preview has expired. Upload or fetch the document again."
            )

        parsed = preview.parsed
        resolved_title = (title or parsed.title).strip() or parsed.title
        source_id = f"{preview.origin}-{_slugify(resolved_title)}"

        registry = SourceRegistry.load()
        if registry.get(source_id) is not None:
            source_id = f"{source_id}-{uuid.uuid4().hex[:6]}"

        # A user document with no destination is not place-specific and will
        # not be returned by a destination-scoped search. Defaulting it to the
        # configured destination would be a guess, so it is left blank unless
        # given.
        place = (destination or "").strip()

        licence = (license_ or "").strip() or (
            USER_SUPPLIED_LICENCE if preview.origin == "upload" else "unknown"
        )
        url = (source_url or preview.source_url or "").strip()
        retrieved_at = utc_now_iso()

        metadata = {
            "source_id": source_id,
            "source_title": resolved_title,
            "source_url": url or f"local://{preview.filename}",
            "publisher": (publisher or "").strip() or "User supplied",
            "license": licence,
            "origin": preview.origin,
            "destination": place,
            "kind": "user",
            "facets": "",
            "retrieved_at": retrieved_at,
        }
        path = settings.kb_dir / f"{source_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            frontmatter.dumps(metadata, parsed.markdown), encoding="utf-8"
        )

        registry.upsert(
            SourceRecord(
                source_id=source_id,
                source_title=resolved_title,
                source_url=metadata["source_url"],
                publisher=metadata["publisher"],
                license=licence,
                origin=preview.origin,
                destination=place,
                kind="user",
                doc_path=relative_doc_path(path),
                retrieved_at=retrieved_at,
                committed=False,
                state="available",
            )
        )
        registry.save()
        logger.info("Added source %s from %s", source_id, preview.origin)

        response = {
            "source_id": source_id,
            "title": resolved_title,
            "destination": place,
            "job": None,
        }
        if rebuild:
            response["job"] = self.start_rebuild(reason=f"added {source_id}")
        return response

    def discard_preview(self, preview_token: str) -> None:
        with self._lock:
            self._previews.pop(preview_token, None)

    # -- remove ------------------------------------------------------------
    def remove_source(self, source_id: str, *, rebuild: bool = True) -> dict:
        registry = SourceRegistry.load()
        record = registry.remove(source_id, delete_document=True)
        if record is None:
            raise IngestionError(f"No source with id {source_id!r}.")
        registry.save()
        logger.info("Removed source %s", source_id)

        response = {"removed": source_id, "job": None}
        if rebuild and registry.available():
            response["job"] = self.start_rebuild(reason=f"removed {source_id}")
        elif rebuild:
            response["note"] = (
                "No sources remain, so the index was not rebuilt. Add a source "
                "to rebuild it."
            )
        return response

    def remove_destination(self, destination: str, *, rebuild: bool = True) -> dict:
        """Remove every document for one place.

        A destination is the unit a user thinks in -- "drop Kyoto" -- and
        removing its documents one by one would rebuild the index once per
        document.
        """
        registry = SourceRegistry.load()
        resolved = registry.resolve_destination(destination)
        if resolved is None:
            known = registry.destination_names()
            raise IngestionError(
                f"No destination called {destination!r} in the knowledge base. "
                f"It covers: {', '.join(known) or 'nothing'}."
            )
        removed = registry.remove_destination(resolved)
        registry.save()
        logger.info("Removed destination %s (%d documents)", resolved, len(removed))

        response = {
            "removed_destination": resolved,
            "removed_documents": [r.source_id for r in removed],
            "job": None,
        }
        if rebuild and registry.available():
            response["job"] = self.start_rebuild(reason=f"removed {resolved}")
        elif rebuild:
            response["note"] = (
                "No sources remain, so the index was not rebuilt."
            )
        return response

    # -- rebuild -----------------------------------------------------------
    def start_rebuild(self, reason: str = "manual") -> dict:
        """Queue a rebuild, or return the one already running."""
        with self._lock:
            active = self._jobs.get(self._active_job_id or "")
            if active and active.state in {"queued", "running"}:
                logger.info("Rebuild already in flight (%s)", active.job_id)
                return active.as_dict()

            job = Job(job_id=f"rebuild-{uuid.uuid4().hex[:8]}", kind="rebuild")
            job.log.append(f"queued ({reason})")
            self._jobs[job.job_id] = job
            self._active_job_id = job.job_id

        thread = threading.Thread(
            target=self._run_rebuild, args=(job.job_id,), daemon=True,
            name=f"ingest-{job.job_id}",
        )
        thread.start()
        return job.as_dict()

    def _run_rebuild(self, job_id: str) -> None:
        job = self._jobs[job_id]
        job.state = "running"

        def progress(message: str) -> None:
            job.stage = message
            job.log.append(message)
            logger.info("[%s] %s", job_id, message)

        try:
            manifest = rebuild_index(progress=progress)
        except Exception as exc:
            job.state = "failed"
            job.stage = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.log.append(f"FAILED: {job.error}")
            # The previous index is untouched -- rebuild_index only swaps on
            # success -- so the app keeps answering from it.
            job.log.append("the previously built index is still in use")
            logger.exception("Rebuild %s failed", job_id)
        else:
            job.state = "succeeded"
            job.stage = "done"
            job.result = manifest
            if self.on_index_rebuilt is not None:
                self.on_index_rebuilt()
                job.log.append("retriever reloaded")
        finally:
            job.finished_at = datetime.now(timezone.utc).replace(
                microsecond=0
            ).isoformat()


#: The application-wide instance.
service = IngestionService()
