"""Lesson 12 - the same six routes in stdlib, then in FastAPI.

    .venv/Scripts/python.exe learn/12-serving/run.py

No API key needed. The "agent" is a stub that sleeps, so this lesson is
about the HTTP layer and nothing else. Imports nothing from app/.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared import rule

STDLIB_PORT = 8771
FASTAPI_PORT = 8772

AGENT_DELAY = 1.5     # pretend an LLM call takes this long


def fake_agent(question: str) -> dict:
    """Stands in for app/agent.py. Slow, like the real thing."""
    time.sleep(AGENT_DELAY)
    return {
        "answer": f"(stub answer to {question!r})",
        "provenance": {"kb_sources": [], "tool_calls": []},
    }


def request(url: str, payload: dict | None = None, timeout: float = 30.0):
    """Returns (status, body)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


# ======================================================================
rule("LEVEL 1 - http.server from the standard library")
# ======================================================================


class StdlibHandler(BaseHTTPRequestHandler):
    """The same routes, by hand. Note everything you have to write."""

    def log_message(self, *args):     # silence the default stderr logging
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Routing is an if-ladder. No path parameters without regex.
        if self.path == "/health":
            self._json(200, {"status": "ok", "llm": {"provider": "stub"}})
        elif self.path == "/conversations":
            self._json(200, {"conversations": []})
        elif self.path == "/":
            self._json(200, {"hint": "would serve index.html"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/chat":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)

        # Everything below is validation FastAPI does for free.
        try:
            body = json.loads(raw)
        except ValueError:
            self._json(400, {"error": "body is not valid JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "body must be an object"})
            return
        question = body.get("question")
        if not isinstance(question, str) or not question.strip():
            self._json(400, {"error": "question must be a non-empty string"})
            return
        if len(question) > 4000:
            self._json(400, {"error": "question too long"})
            return

        result = fake_agent(question)
        result["session_id"] = body.get("session_id") or "generated"
        self._json(200, result)


# HTTPServer, not ThreadingHTTPServer -- deliberately, to show the problem.
stdlib_server = HTTPServer(("127.0.0.1", STDLIB_PORT), StdlibHandler)
threading.Thread(target=stdlib_server.serve_forever, daemon=True).start()
print(f"  serving on http://127.0.0.1:{STDLIB_PORT}  (~80 lines of handler)")

print()
print("  it works:")
status, body = request(f"http://127.0.0.1:{STDLIB_PORT}/health")
print(f"    GET  /health   -> {status}  {body}")
status, body = request(f"http://127.0.0.1:{STDLIB_PORT}/chat",
                       {"question": "where should I eat?"})
print(f"    POST /chat     -> {status}  {body[:90]}")

print()
print("  and the hand-written validation works too:")
for label, payload in (
    ("missing question", {"session_id": "x"}),
    ("wrong type", {"question": 42}),
    ("empty", {"question": "   "}),
):
    status, body = request(f"http://127.0.0.1:{STDLIB_PORT}/chat", payload)
    print(f"    {label:<18} -> {status}  {body[:70]}")

print()
print("  ...but I had to write 18 lines to get those four checks, and")
print("  they return a bare string error with no field name. Miss one")
print("  and you get a 500 with a traceback in the log.")


# ======================================================================
rule("THE PROBLEM THAT ACTUALLY MATTERS - one slow request blocks all")
# ======================================================================

print(f"  HTTPServer is SINGLE-THREADED. Our stub agent sleeps "
      f"{AGENT_DELAY}s,")
print("  which is realistic - a real chat turn is 2-10s of LLM, retrieval")
print("  and tool calls.")
print()
print("  Firing 4 concurrent /chat requests:")
print()

results: list[tuple[int, float]] = []
lock = threading.Lock()


def hammer(index: int, port: int) -> None:
    start = time.perf_counter()
    status, _ = request(f"http://127.0.0.1:{port}/chat",
                        {"question": f"question {index}"})
    with lock:
        results.append((index, time.perf_counter() - start))


started = time.perf_counter()
threads = [threading.Thread(target=hammer, args=(i, STDLIB_PORT))
           for i in range(4)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
stdlib_wall = time.perf_counter() - started

for index, elapsed in sorted(results):
    bar = "#" * int(elapsed * 20)
    print(f"    request {index}  waited {elapsed:>5.2f}s  {bar}")
print()
print(f"  total wall time: {stdlib_wall:.2f}s")
print(f"  ideal (fully concurrent): ~{AGENT_DELAY:.2f}s")
print(f"  actual: ~{stdlib_wall / AGENT_DELAY:.1f}x the ideal - they queued.")
print()
print("  ** Four users, and the fourth waits for the first three. **")
print()
print("  ThreadingHTTPServer fixes the blocking but gives you a THREAD")
print("  per request - and app/agent.py is `async`, so you would be")
print("  bridging sync and async on every single call.")

stdlib_server.shutdown()


# ======================================================================
rule("LEVEL 2 - FastAPI")
# ======================================================================

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="learn-12", version="0.1")


class ChatRequest(BaseModel):
    """The validation. This class IS the validation."""

    session_id: str | None = None
    question: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    provenance: dict


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "llm": {"provider": "stub"}}


@app.get("/conversations")
async def conversations() -> dict:
    return {"conversations": []}


@app.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest) -> ChatResponse:
    import asyncio

    # await, not time.sleep: this is what frees the event loop.
    await asyncio.sleep(AGENT_DELAY)
    return ChatResponse(
        answer=f"(stub answer to {payload.question!r})",
        session_id=payload.session_id or "generated",
        provenance={"kb_sources": [], "tool_calls": []},
    )


config = uvicorn.Config(app, host="127.0.0.1", port=FASTAPI_PORT,
                        log_level="error")
server = uvicorn.Server(config)
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
for _ in range(80):                       # wait for it to come up
    status, _ = request(f"http://127.0.0.1:{FASTAPI_PORT}/health", timeout=1)
    if status == 200:
        break
    time.sleep(0.1)

print(f"  serving on http://127.0.0.1:{FASTAPI_PORT}")
print()
print("  the entire /chat route:")
print("""
      class ChatRequest(BaseModel):
          session_id: str | None = None
          question: str = Field(min_length=1, max_length=4000)

      @app.post("/chat", response_model=ChatResponse)
      async def chat(payload: ChatRequest) -> ChatResponse:
          ...
""")
print("  The annotation `payload: ChatRequest` IS the validation.")
print("  Zero lines of validation code.")

print()
print("  the same bad requests, unhandled by me:")
print()
for label, payload in (
    ("missing question", {"session_id": "x"}),
    ("wrong type", {"question": 42}),
    ("empty", {"question": ""}),
    ("too long", {"question": "x" * 5000}),
):
    status, body = request(f"http://127.0.0.1:{FASTAPI_PORT}/chat", payload)
    try:
        detail = json.loads(body)["detail"][0]
        summary = (f"loc={detail.get('loc')} type={detail.get('type')} "
                   f"msg={detail.get('msg')[:40]}")
    except Exception:
        summary = body[:80]
    print(f"    {label:<18} -> {status}  {summary}")

print()
print("  ** 422 with the FIELD NAME, the rule that failed and a message,")
print("     for free. ** Compare the bare strings from level 1.")
print()
print("  And note: that is the same pydantic from lesson 01, doing the")
print("  same job - declare the shape, let the library enforce it - on")
print("  HTTP bodies instead of .env values. Learn pydantic once, use")
print("  it three times in this codebase.")


# ======================================================================
rule("CONCURRENCY, REDONE")
# ======================================================================

results.clear()
started = time.perf_counter()
threads = [threading.Thread(target=hammer, args=(i, FASTAPI_PORT))
           for i in range(4)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
fastapi_wall = time.perf_counter() - started

for index, elapsed in sorted(results):
    bar = "#" * int(elapsed * 20)
    print(f"    request {index}  waited {elapsed:>5.2f}s  {bar}")

print()
print(f"    {'server':<28} {'wall time':>10} {'vs ideal':>10}")
print(f"    {'-' * 28} {'-' * 10} {'-' * 10}")
print(f"    {'http.server (sync)':<28} {stdlib_wall:>9.2f}s "
      f"{stdlib_wall / AGENT_DELAY:>9.1f}x")
print(f"    {'FastAPI + uvicorn (async)':<28} {fastapi_wall:>9.2f}s "
      f"{fastapi_wall / AGENT_DELAY:>9.1f}x")
print()
print("  ONE process, ONE thread, four concurrent slow requests, served")
print("  in about the time of one.")
print()
print("  Why: `await` YIELDS THE EVENT LOOP while the slow thing is in")
print("  flight. For an app whose requests are dominated by waiting on")
print("  network I/O - an LLM call, an MCP tool, an HTTP fetch - this is")
print("  exactly the right model.")
print()
print("  ** It is also why app/api.py's routes are `async def` and")
print("     app/agent.py's ask() is a coroutine. That is not fashion. **")


# ======================================================================
rule("FREE THINGS - the OpenAPI schema")
# ======================================================================

status, body = request(f"http://127.0.0.1:{FASTAPI_PORT}/openapi.json")
schema = json.loads(body)
print(f"  GET /openapi.json -> {status}, {len(body):,} bytes")
print()
print(f"  paths documented: {list(schema.get('paths', {}))}")
print(f"  schemas derived : {list(schema.get('components', {}).get('schemas', {}))}")
print()
print("  ChatRequest, as FastAPI documented it from the class:")
for line in json.dumps(
    schema["components"]["schemas"]["ChatRequest"], indent=2
).splitlines():
    print("      " + line)
print()
print("  Plus interactive docs at /docs and /redoc, generated from the")
print("  same annotations. Nobody wrote any of this.")


# ======================================================================
rule("app/api.py - the parts worth reading")
# ======================================================================
print("""
  1. LIFESPAN. Building the agent connects two MCP subprocesses and
     loads a 130 MB embedding model - seconds of work. The lifespan
     handler does it ONCE at startup and shares it. Per-request, every
     chat turn would pay for it.

  2. TOLERANT STARTUP. A missing index or a dead MCP server leaves the
     app SERVING WITH REDUCED CAPABILITY rather than refusing to boot.
     If the agent cannot be built at all, _startup_error is recorded
     and reported by /health and by a clear error on /chat - not a
     stack trace on every request.

     For a demo that is exactly right: an app that boots and tells you
     what is broken beats one that exits with a traceback.

  3. AsyncExitStack. The subtle bit:

         saver = await _resources.enter_async_context(
             AsyncSqliteSaver.from_conn_string(...))

     from_conn_string is an async context manager, but the connection
     must OUTLIVE the function that creates it. `async with` would
     close it on return. So it is entered on a process-wide stack,
     closed during shutdown.

  4. /health REPORTS CONFIGURATION TRAPS, not just liveness -
     including the .env-shadowed-by-shell conflict from lesson 01.
     (This machine currently has that conflict.) A health endpoint
     that surfaces config problems is the difference between five
     minutes of debugging and an hour.

  5. ROUTE ORDER. Mounting StaticFiles at "/" would shadow every API
     route below it, because the first match wins. Hence the explicit
     "/" handler and the mount confined to "/static".
""")


# ======================================================================
rule("WHAT IS DELIBERATELY NOT THERE")
# ======================================================================
print("""
  Not criticisms of a demo - just the honest list:

    auth              every route is open
    rate limiting     /chat calls a metered API with no per-user cap
    CORS              no CORSMiddleware, so a browser on another
                      origin cannot call it
    request ids       no correlation id to trace a bad answer back
    STREAMING         /chat waits for the whole answer. Token
                      streaming (SSE or WebSocket) is the single
                      biggest PERCEIVED performance win available,
                      because the answer takes seconds
    upload safety     size and extension only; no content sniffing

  And one deployment trap worth knowing before it bites:

    `gunicorn -w 4` means FOUR PROCESSES, each of which would
      - build its own agent: 4 embedding models in RAM,
        8 MCP subprocesses
      - open the SAME SQLite file - and SQLite is single-writer
        (lesson 11)

    The fix is one worker with many async connections (fine here,
    since the load is I/O-bound), or PostgresSaver plus a shared
    index.

Next: learn/13-mini-app/README.md
""")

server.should_exit = True
