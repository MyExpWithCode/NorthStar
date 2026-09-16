"""End-to-end test of the ingestion service.

    python scripts/smoke_ingest_service.py

Proves the whole loop a reviewer will exercise in the /admin UI: upload a
document, see the preview, confirm it, wait for the rebuild, then retrieve a
fact that can ONLY have come from that document. Then remove it and confirm the
fact stops being retrievable.

Also proves the two safety invariants: a failed rebuild leaves the working index
serving, and a second rebuild request joins the in-flight job instead of racing
it.
"""

from __future__ import annotations

import sys as _sys

for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import io
import sys
import time

from app.ingest import build_index
from app.ingest.registry import SourceRegistry
from app.ingest.service import IngestionError, service
from app.rag import retriever

#: A fact that appears nowhere in the curated knowledge base, so retrieving it
#: proves the uploaded document reached the index.
CANARY = "The Bukit Zarquon Observation Deck"
DOC_TEXT = (
    "# Bukit Zarquon Visitor Guide\n\n"
    "## Overview\n\n"
    f"{CANARY} is a fictional viewing platform used to test document "
    "ingestion. It sits 91 metres above the Kallang basin and is reached by "
    "a short covered walkway from the nearest station. Entry costs 14 dollars "
    "for adults and 7 dollars for children under twelve.\n\n"
    "## Opening hours\n\n"
    "The deck opens daily from 9am until 10pm, with last entry at 9:30pm. "
    "It is fully sheltered, making it a practical option in wet weather. "
    "A cafe on the upper level serves kaya toast and local coffee.\n"
)


def wait_for_job(job_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    last_stage = None
    while time.time() < deadline:
        job = service.job(job_id)
        if job["stage"] != last_stage:
            print(f"       [{job['state']}] {job['stage']}")
            last_stage = job["stage"]
        if job["state"] in {"succeeded", "failed"}:
            return job
        time.sleep(1.0)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def retrievable(text: str) -> bool:
    retriever.reload()
    hits = retriever.search(text, k=5)
    return any(text.lower() in hit.document.page_content.lower() for hit in hits)


def main() -> int:
    failures: list[str] = []
    service.on_index_rebuilt = retriever.reload

    print("=" * 78)
    print("BASELINE")
    print("=" * 78)
    status = service.status()
    print(f"  sources      : {status['source_count']}")
    print(f"  total chunks : {status['total_chunks']}")
    print(f"  index chunks : {status['index']['chunk_count']}")
    baseline_chunks = status["index"]["chunk_count"]
    print(f"  canary already retrievable: {retrievable(CANARY)} (must be False)")
    if retrievable(CANARY):
        failures.append("canary text was already in the index")

    print()
    print("=" * 78)
    print("UPLOAD -> PREVIEW -> CONFIRM")
    print("=" * 78)
    preview = service.preview_upload(DOC_TEXT.encode("utf-8"), "bukit-zarquon.md")
    print(f"  preview token : {preview['preview_token']}")
    print(f"  title         : {preview['title']}")
    print(f"  format        : {preview['format']}  chars={preview['char_count']}")
    print(f"  headings      : {preview['headings']}")
    print(f"  licence offer : {preview['suggested_license']}")
    print(f"  committed yet : {SourceRegistry.load().get('upload-bukit-zarquon-visitor-guide') is not None}"
          " (must be False -- preview does not commit)")
    if SourceRegistry.load().get("upload-bukit-zarquon-visitor-guide"):
        failures.append("preview committed the document")

    result = service.confirm(
        preview["preview_token"],
        publisher="Smoke test",
        license_="user-supplied",
    )
    source_id = result["source_id"]
    print(f"  confirmed as  : {source_id}")
    job = wait_for_job(result["job"]["job_id"])
    if job["state"] != "succeeded":
        failures.append(f"rebuild failed: {job['error']}")
        print(f"  rebuild FAILED: {job['error']}")
        return 1
    print(f"  index chunks  : {baseline_chunks} -> {job['result']['chunk_count']}")
    print(f"  job log       : {job['log']}")

    found = retrievable(CANARY)
    print(f"  canary retrievable now: {found} (must be True)")
    if not found:
        failures.append("uploaded document did not become retrievable")
    else:
        hit = retriever.search(CANARY, k=1)[0]
        print(f"  cited as      : {hit.metadata['source_title']} > "
              f"{hit.metadata['section_path'].split(' > ')[-1]}")
        print(f"  licence       : {hit.metadata['license']}")
        print(f"  relevance     : {hit.score:.3f}")

    print()
    print("=" * 78)
    print("CONCURRENT REBUILDS MUST NOT RACE")
    print("=" * 78)
    first = service.start_rebuild(reason="test-a")
    second = service.start_rebuild(reason="test-b")
    same = first["job_id"] == second["job_id"]
    print(f"  first : {first['job_id']}")
    print(f"  second: {second['job_id']}")
    print(f"  joined the in-flight job: {same} (must be True)")
    if not same:
        failures.append("a second rebuild started a competing job")
    wait_for_job(first["job_id"])

    print()
    print("=" * 78)
    print("A FAILED REBUILD MUST LEAVE THE WORKING INDEX SERVING")
    print("=" * 78)
    before = build_index.read_manifest()
    original = build_index.rebuild_index
    import app.ingest.service as service_module

    def boom(*args, **kwargs):
        raise RuntimeError("simulated embedding failure")

    service_module.rebuild_index = boom
    try:
        job = wait_for_job(service.start_rebuild(reason="test-failure")["job_id"])
    finally:
        service_module.rebuild_index = original

    print(f"  job state     : {job['state']} (must be failed)")
    print(f"  error         : {job['error']}")
    print(f"  log           : {job['log'][-1]}")
    after = build_index.read_manifest()
    print(f"  manifest unchanged      : {after == before}")
    print(f"  canary still retrievable: {retrievable(CANARY)}")
    if job["state"] != "failed":
        failures.append("a failing rebuild did not report failure")
    if after != before:
        failures.append("a failed rebuild damaged the working index")

    print()
    print("=" * 78)
    print("REMOVE -> THE FACT MUST STOP BEING RETRIEVABLE")
    print("=" * 78)
    removal = service.remove_source(source_id)
    print(f"  removed       : {removal['removed']}")
    job = wait_for_job(removal["job"]["job_id"])
    print(f"  index chunks  : {job['result']['chunk_count']} "
          f"(baseline was {baseline_chunks})")
    still = retrievable(CANARY)
    print(f"  canary retrievable: {still} (must be False)")
    if still:
        failures.append("removed document is still retrievable")
    if (settings_path := (build_index.settings.kb_dir / f"{source_id}.md")).exists():
        failures.append(f"document file {settings_path.name} was not deleted")
    else:
        print("  document file deleted: True")
    if SourceRegistry.load().get(source_id) is not None:
        failures.append("registry entry survived removal")
    else:
        print("  registry entry removed: True")
    if job["result"]["chunk_count"] != baseline_chunks:
        failures.append(
            f"chunk count did not return to baseline "
            f"({job['result']['chunk_count']} vs {baseline_chunks})"
        )

    print()
    print("=" * 78)
    print("REJECTIONS")
    print("=" * 78)
    for label, call in [
        ("oversized upload",
         lambda: service.preview_upload(b"x" * (build_index.settings.max_upload_bytes + 1),
                                        "huge.md")),
        ("scanned/empty document",
         lambda: service.preview_upload(b"# Hi", "tiny.md")),
        ("unsupported type",
         lambda: service.preview_upload(b"x" * 500, "thing.zip")),
        ("not a URL", lambda: service.preview_url("ftp://example.com/x")),
        ("expired preview token", lambda: service.confirm("deadbeefdead")),
        ("unknown source id", lambda: service.remove_source("no-such-source")),
    ]:
        try:
            call()
        except IngestionError as exc:
            print(f"  OK   {label}: {str(exc)[:110]}")
        except Exception as exc:
            print(f"  FAIL {label}: raised {type(exc).__name__}: {exc}")
            failures.append(f"{label} raised the wrong error")
        else:
            print(f"  FAIL {label} was accepted")
            failures.append(f"{label} was not rejected")

    print()
    print("=" * 78)
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All ingestion service checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
