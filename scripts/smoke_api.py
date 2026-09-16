"""Exercise every HTTP endpoint through the real application.

    python scripts/smoke_api.py

Uses FastAPI's TestClient, which runs the actual lifespan handler -- so this
also proves the MCP subprocesses start and the index loads on boot.

Chat turns are skipped when the LLM provider is out of quota; everything else
(health, sources, admin, error shapes) runs regardless, so this stays a useful
check even without LLM budget.
"""

from __future__ import annotations

import sys as _sys

for _stream in (_sys.stdout, _sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import json
import sys
import time

from fastapi.testclient import TestClient

from app.api import app

RATE_LIMIT_MARKER = "rate-limited right now"
CANARY = "The Bukit Zarquon Observation Deck"
DOC = (
    "# Bukit Zarquon Visitor Guide\n\n## Overview\n\n"
    f"{CANARY} is a fictional viewing platform used to test ingestion through "
    "the HTTP API. It sits 91 metres above the Kallang basin, is fully "
    "sheltered, and costs 14 dollars for adults.\n\n## Opening hours\n\n"
    "Open daily 9am to 10pm with last entry at 9:30pm. A cafe on the upper "
    "level serves kaya toast and local coffee all day.\n"
)


def main() -> int:
    failures: list[str] = []
    skipped: list[str] = []

    print("starting application (runs lifespan: MCP servers + index)...")
    with TestClient(app) as client:
        print()
        print("=" * 78)
        print("GET /health")
        print("=" * 78)
        health = client.get("/health").json()
        print(f"  status        : {health['status']}")
        print(f"  llm           : {health['llm']['provider']} / "
              f"{health['llm'].get('model')}")
        kb = health["knowledge_base"]
        print(f"  kb ready      : {kb['ready']}  chunks="
              f"{(kb['manifest'] or {}).get('chunk_count')}  "
              f"floor={kb['relevance_floor']}")
        print(f"  mcp servers   : {health['mcp']['servers']}")
        print(f"  mcp degraded  : {health['mcp']['degraded']}")
        if not kb["ready"]:
            failures.append("health reports the knowledge base as not ready")
        if len(health["mcp"]["servers"]) != 2:
            failures.append(f"expected 2 MCP servers, got {health['mcp']['servers']}")
        if health["agent"] is None:
            failures.append("agent did not start")

        print()
        print("=" * 78)
        print("GET /sources")
        print("=" * 78)
        sources = client.get("/sources").json()
        licences = sorted({s["license"] for s in sources["sources"]})
        print(f"  registered    : {len(sources['sources'])}")
        print(f"  available     : {sources['available']}")
        print(f"  total chunks  : {sources['total_chunks']}")
        print(f"  licences      : {licences}")
        if sources["available"] < 3:
            failures.append("fewer than 3 available sources")

        print()
        print("=" * 78)
        print("GET /admin/status")
        print("=" * 78)
        status = client.get("/admin/status").json()
        print(f"  index ready   : {status['index_ready']}")
        print(f"  sources       : {status['source_count']}")
        print(f"  uploads       : {status['supported_uploads']} "
              f"(max {status['max_upload_mb']} MB)")
        print(f"  active job    : {status['active_job']}")

        print()
        print("=" * 78)
        print("ADMIN: upload -> preview -> confirm -> rebuild -> remove")
        print("=" * 78)
        response = client.post(
            "/admin/sources/upload",
            files={"file": ("api-canary.md", DOC.encode(), "text/markdown")},
        )
        if response.status_code != 200:
            failures.append(f"upload preview failed: {response.text[:200]}")
            print(f"  FAIL {response.status_code}: {response.text[:200]}")
        else:
            preview = response.json()
            print(f"  preview       : {preview['title']} "
                  f"[{preview['format']}, {preview['char_count']} chars]")
            print(f"  headings      : {preview['headings']}")

            confirmed = client.post(
                "/admin/sources/confirm",
                json={
                    "preview_token": preview["preview_token"],
                    "publisher": "API smoke test",
                    "license": "user-supplied",
                },
            ).json()
            source_id = confirmed["source_id"]
            job_id = confirmed["job"]["job_id"]
            print(f"  confirmed     : {source_id}")

            deadline = time.time() + 300
            job = None
            while time.time() < deadline:
                job = client.get(f"/admin/jobs/{job_id}").json()
                if job["state"] in {"succeeded", "failed"}:
                    break
                time.sleep(1)
            print(f"  rebuild       : {job['state']} -- {job['stage']}")
            if job["state"] != "succeeded":
                failures.append(f"rebuild failed: {job.get('error')}")
            else:
                print(f"  chunks        : {job['result']['chunk_count']}")

            removed = client.delete(f"/admin/sources/{source_id}").json()
            print(f"  removed       : {removed['removed']}")
            job_id = removed["job"]["job_id"]
            deadline = time.time() + 300
            while time.time() < deadline:
                job = client.get(f"/admin/jobs/{job_id}").json()
                if job["state"] in {"succeeded", "failed"}:
                    break
                time.sleep(1)
            print(f"  re-index      : {job['state']}, "
                  f"{(job.get('result') or {}).get('chunk_count')} chunks")

        print()
        print("=" * 78)
        print("ERROR SHAPES -- useful messages, not stack traces")
        print("=" * 78)
        cases = [
            ("POST /chat with an empty message", "post", "/chat",
             {"json": {"message": ""}}, 422),
            ("POST /admin/sources/url with a non-URL", "post",
             "/admin/sources/url", {"json": {"url": "not-a-url"}}, 400),
            ("POST /admin/sources/confirm with a dead token", "post",
             "/admin/sources/confirm", {"json": {"preview_token": "dead"}}, 400),
            ("DELETE an unknown source", "delete", "/admin/sources/nope", {}, 400),
            ("GET an unknown job", "get", "/admin/jobs/nope", {}, 400),
            ("GET a route that does not exist", "get", "/nope", {}, 404),
        ]
        for label, method, path, kwargs, expected in cases:
            response = getattr(client, method)(path, **kwargs)
            body = response.text[:120].replace("\n", " ")
            ok = response.status_code == expected
            print(f"  {'OK  ' if ok else 'FAIL'} {label}: "
                  f"{response.status_code} {body}")
            if not ok:
                failures.append(
                    f"{label}: expected {expected}, got {response.status_code}"
                )

        print()
        print("=" * 78)
        print("POST /chat")
        print("=" * 78)
        reply = client.post(
            "/chat",
            json={"message": "What are the must-visit attractions in Singapore?"},
        )
        if reply.status_code != 200:
            failures.append(f"/chat returned {reply.status_code}: {reply.text[:200]}")
            print(f"  FAIL {reply.status_code}: {reply.text[:200]}")
        else:
            payload = reply.json()
            if RATE_LIMIT_MARKER in payload["answer"]:
                print("  SKIPPED -- LLM provider out of quota")
                skipped.append("chat turns")
            else:
                session = payload["session_id"]
                print(f"  session       : {session}")
                print(f"  kb_sources    : {len(payload['kb_sources'])}")
                print(f"  tool_calls    : "
                      f"{[c['tool'] for c in payload['tool_calls']]}")
                print(f"  degraded      : {payload['degraded_tools']}")
                print(f"  answer        : {' '.join(payload['answer'].split())[:180]}...")
                if not payload["kb_sources"]:
                    failures.append("/chat returned no kb_sources")
                else:
                    first = payload["kb_sources"][0]
                    for key in ("marker", "title", "url", "section_path", "score"):
                        if key not in first:
                            failures.append(f"kb_source missing {key}")
                    print(f"  citation      : [{first['marker']}] {first['title']} "
                          f"-> {first['url'][:50]}")

                # Memory: the second turn must see the first.
                followup = client.post(
                    "/chat",
                    json={"message": "Which of those is indoors?",
                          "session_id": session},
                ).json()
                if RATE_LIMIT_MARKER in followup["answer"]:
                    print("  follow-up SKIPPED -- out of quota")
                    skipped.append("multi-turn over HTTP")
                else:
                    stored = client.get(f"/history/{session}").json()["messages"]
                    print(f"  history       : {len(stored)} messages retained")
                    if len(stored) < 3:
                        failures.append("history did not retain the conversation")
                    reset = client.post("/reset", json={"session_id": session}).json()
                    after = client.get(f"/history/{session}").json()["messages"]
                    print(f"  after reset   : {len(after)} messages "
                          f"(reset={reset['reset']})")

    print()
    print("=" * 78)
    if skipped:
        print(f"{len(skipped)} check(s) skipped (LLM provider quota): {skipped}")
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    if skipped:
        return 2
    print("All API checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
