# 12 — Serving: what FastAPI adds over `http.server`

**Real file:** [app/api.py](../../app/api.py)
**Libraries:** `fastapi`, `uvicorn`, `python-multipart`, `pydantic`

No API key needed.

## The job

```
POST /chat  {"session_id": "abc", "question": "..."}
   -> {"answer": "...", "provenance": {...}, "session_id": "abc"}

GET  /health          is anything broken?
GET  /conversations   list past chats
POST /admin/upload    accept a PDF
GET  /                serve index.html
```

Six routes. You could write it with `http.server` from the standard library,
and `run.py` does — in about 80 lines, with every shortcoming visible.

## Level 1 — stdlib, and what breaks

```python
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        question = body["question"]          # KeyError -> HTTP 500
        ...
```

Everything you have to write yourself:

| Missing | What it costs |
|---|---|
| routing | an `if self.path == ...` ladder |
| JSON parsing | manual, and a bad body is an unhandled 500 |
| **validation** | `body["question"]` on a missing key → 500, not 422 |
| **concurrency** | `HTTPServer` is single-threaded: one slow LLM call blocks every other user |
| async | none. Your `async def` agent needs `asyncio.run` per request |
| file uploads | multipart/form-data parsed by hand. Genuinely unpleasant |
| static files | open, guess the MIME type, set headers |
| docs | none |

The one that actually matters for this app is **concurrency**. A chat turn takes
2–10 seconds (LLM + retrieval + tools). `ThreadingHTTPServer` fixes the
blocking but gives you a thread per request, and your agent is `async` —
so you would be bridging sync and async on every call.

## Level 2 — FastAPI

```python
class ChatRequest(BaseModel):
    session_id: str | None = None
    question: str = Field(min_length=1, max_length=4000)

@app.post("/chat")
async def chat(request: ChatRequest) -> dict:
    answer, provenance = await agent_module.ask(
        _agent, request.session_id or str(uuid.uuid4()), request.question)
    return {"answer": answer, "provenance": provenance.as_dict()}
```

The type annotation `request: ChatRequest` **is** the validation. FastAPI reads
it, parses the body, validates against the pydantic model, and returns a 422
with a field-level error if it fails. You write zero validation code.

That is the same pydantic from lesson 01, doing the same job — declare the
shape, let the library enforce it — on HTTP bodies instead of `.env` values.
Learning pydantic once pays off three times in this codebase.

And `async def` means one worker handles many concurrent slow requests, because
`await` yields the event loop while the LLM call is in flight. For an app whose
requests are dominated by waiting on network I/O, this is the right model.

## Lifespan: the most important 30 lines in `api.py`

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent, _startup_error
    try:
        checkpointer = await _make_checkpointer()
        _agent = await agent_module.build_agent(checkpointer)
    except Exception as exc:
        _startup_error = str(exc)
    yield
    await _resources.aclose()
```

Why this exists: **starting the agent is expensive.** It connects two MCP
subprocesses and loads a 130 MB embedding model — seconds of work. Do it
per-request and every chat turn pays for it. The lifespan handler runs it once
at startup and shares the result.

Two decisions inside it worth noting:

**1. Startup is tolerant.** A missing index or an unreachable MCP server leaves
the app *serving with reduced capability* rather than refusing to boot. If the
agent cannot be built at all, `_startup_error` is recorded and reported by
`/health` and by a clear error on `/chat` — **not a stack trace on every
request.**

For a demo this is exactly right: an app that boots and tells you what is
broken is far more useful than one that exits with a traceback.

**2. The `AsyncExitStack`.** This is the subtle bit:

```python
_resources = AsyncExitStack()
...
saver = await _resources.enter_async_context(
    AsyncSqliteSaver.from_conn_string(str(settings.conversation_db)))
```

`from_conn_string` is an async context manager, but the connection has to
outlive the function that creates it. You cannot write `async with` — that
would close it on return. So it is entered on a process-wide stack which is
closed during shutdown. Small, and genuinely tricky lifetime management.

## `/health`: the honest-status endpoint

```python
{"llm": {...}, "index": {...}, "tools": {...}, "degraded_tools": [...],
 "dotenv_shadowed": [...]}
```

It reports the LLM provider and whether a key is configured (**never the key**
— lesson 01's `SecretStr`), the index manifest, which MCP servers came up, and
the `.env` shadowing conflict from lesson 01.

That last one is unusual and good. A health endpoint that reports
*configuration traps*, not just liveness, is the difference between five
minutes of debugging and an hour. This machine currently *has* that conflict —
lesson 01 detected it.

## Static files: two mounts, and why the order matters

```python
app.mount("/static", StaticFiles(directory=settings.static_dir))

@app.get("/")
async def index():
    return FileResponse(settings.static_dir / "index.html")
```

Route order matters in FastAPI — the first match wins. Mounting `StaticFiles`
at `/` would shadow every API route below it. Hence the explicit `/` handler
and the mount confined to `/static`.

`app/static/` is two hand-written HTML files with inline JS and no build step.
For this app that is the right call: no `node_modules`, no bundler, no npm
install in the setup instructions. It would be the wrong call at ten pages.

## Alternatives

| Framework | Async | Validation | When |
|---|---|---|---|
| `http.server` | no | no | learning, or a 20-line utility |
| **Flask** | no (2.x has some) | manual | simple sync apps, huge ecosystem |
| **FastAPI** | **yes** | **pydantic** | **used here.** The default for a JSON API in 2026 |
| Starlette | yes | no | FastAPI without the pydantic layer; lighter |
| Litestar | yes | pydantic/attrs | FastAPI-like, opinionated, fast |
| Django + DRF | partial | serialisers | you want an ORM, admin and auth included |
| Quart | yes | manual | async Flask, for porting Flask code |

### Servers
| Server | Notes |
|---|---|
| **uvicorn** | **used here.** ASGI, fast, the standard pairing |
| hypercorn | ASGI + HTTP/2 + HTTP/3 |
| granian | Rust-based ASGI, newer, quick |
| gunicorn + uvicorn workers | the usual production setup: multiple processes |

**One thing to watch if you deploy this.** `gunicorn -w 4` means four
processes, and each would:

- build its own agent (4 embedding models in RAM, 8 MCP subprocesses)
- open the **same SQLite file** — and SQLite is single-writer (lesson 11)

The fix is one worker with many async connections (fine here, since the load is
I/O-bound), or `PostgresSaver` plus a shared index. Worth knowing before it
surprises you.

## What is deliberately not here

Honest list of what a production version would need, none of which is a
criticism of a demo:

- **auth** — every route is open
- **rate limiting** — `/chat` calls a metered API with no per-user cap
- **CORS** — no `CORSMiddleware`, so a browser on another origin cannot call it
- **request logging / IDs** — no correlation id to trace a bad answer
- **streaming** — `/chat` waits for the whole answer. Token streaming
  (SSE or WebSocket) is the single biggest *perceived* performance win
  available, since the answer takes seconds
- **upload safety beyond size/extension** — no content sniffing, no AV

## Run it

```bash
.venv/Scripts/python.exe learn/12-serving/run.py
```

It starts a stdlib `http.server` implementation of the same routes, makes real
requests against it, and shows each shortcoming concretely — including a
**demonstration that a single slow request blocks all others**. Then it starts
a FastAPI equivalent, makes the same requests, shows the automatic 422
validation error, and prints the OpenAPI schema FastAPI generated for free.

Nothing calls an LLM; the "agent" is a stub that sleeps.

## Next

[13 — The mini app](../13-mini-app/) — all of it, one file, no LangChain.
