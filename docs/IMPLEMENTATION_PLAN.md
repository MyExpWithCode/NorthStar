# AI Travel Planning Assistant — Implementation Plan

Plan of record, kept in the repo. Tasks run **one at a time**, each ending with its own verification step.
Design is fixed first in [ARCHITECTURE.md](ARCHITECTURE.md); code follows it.

## Progress

| Task | Description | Status |
|---|---|---|
| T0 | Architecture diagrams & design doc | ✅ done — [ARCHITECTURE.md](ARCHITECTURE.md) |
| T1 | Scaffold, config, dependency workflow | ✅ done — `app/config.py`, `pyproject.toml`, `.env.example` |
| T2 | Source registry + knowledge-base acquisition | ✅ done — 15 docs, 522 KB, `sources.json` |
| T3 | Heading-aware chunking + category tagging | ✅ done — 926 chunks, 11 tags, 0.1% untagged |
| T4 | Embeddings + FAISS index + atomic swap | ✅ done — 926 vectors, 384-dim, floor calibrated |
| T5 | Retriever + grounding guard + index reload | ⬜ |
| T6 | MCP server 1 — weather | ⬜ |
| T7 | MCP server 2 — currency | ⬜ |
| T8 | MCP client wiring | ⬜ |
| T9 | LLM factory | ⬜ |
| T10 | Prompt engineering | ⬜ |
| T11 | Agent assembly | ⬜ |
| T12 | Document parsers — md/txt/html/pdf/docx | ⬜ |
| T13 | Ingestion service + job runner | ⬜ |
| T14 | FastAPI backend — chat + admin routes | ⬜ |
| T15 | Chat UI | ⬜ |
| T16 | Ingestion UI (`/admin`) | ⬜ |
| T17 | Failure-path hardening | ⬜ |
| T18 | Sample Q&A document | ⬜ |
| T19 | README + acceptance checklist | ⬜ |
| T20 | Demo materials | ⬜ |

---

## Context

`AI_Travel_Planning_Assistant_Assignment.pdf` (Developer Assignment Brief, Technology Band III) asks for a
context-aware Singapore travel assistant that combines a **document-based RAG knowledge base** (stable
destination facts) with **current information fetched through MCP tools** (weather, currency), and can
**blend both** in a single answer — the flagship scenario being *"Create a three-day Singapore itinerary for
next week and adjust it according to the weather forecast."*

The repo was empty apart from a one-line `README.md`, so this is greenfield. Deliverables: source code, a
working application, the KB documents (or instructions to obtain them), a README covering architecture /
sources / RAG workflow / MCP tools / prompt & context strategy / setup, a sample Q&A document, and a short
demo.

The outcome to aim at: every claim in an answer is attributable to one of three labelled origins —
**knowledge base** (title + URL), **MCP tool** (tool name + timestamp), or **AI suggestion**.

### Environment facts established up front
- Only Python **3.14.3** is installed (`C:\Python314`). `uv 0.11.7` and Node 26 available.
- All required packages resolve for 3.14: `faiss-cpu 1.15.0`, `langchain 1.4.0`, `langgraph 1.2.11`,
  `mcp 2.2.0`, `langchain-mcp-adapters 0.3.2`, `fastembed 0.8.0`, `langchain-groq 1.1.3`,
  `langchain-anthropic 1.7.2`.
- `GROQ_API_KEY` and `LANGSMITH_API_KEY` are already set. `ANTHROPIC_API_KEY` is **not**.
- **LangChain is at 1.x** — LangGraph-backed, `langchain.agents.create_agent`. The `AgentExecutor` /
  `initialize_agent` patterns in most tutorials are gone. Any task touching LangChain verifies the API
  against the installed package before writing code, not from memory.

### Confirmed decisions
| Choice | Decision |
|---|---|
| LLM | Pluggable factory, **Groq default** (`LLM_PROVIDER=groq\|anthropic`) |
| Embeddings | **FastEmbed** ONNX `BAAI/bge-small-en-v1.5` — local, keyless, no torch |
| Vector store | **FAISS**, local files (assumption: no server process needed; `langchain-community` also provides FastEmbed bindings) |
| MCP tools | **Two locally-authored stdio MCP servers** — Open-Meteo weather, Frankfurter currency |
| Chat UI | **FastAPI + plain HTML/JS** |
| Ingestion UI | **`/admin` page in the same app**: index status, add-by-URL, file upload, rebuild, remove |
| Upload formats | md / txt / html / **pdf** / **docx** |

### Dependency discipline
Nothing is installed up front. Each task installs only what it needs with `uv pip install` and appends the
resolved pin to `requirements.txt`.

### Licensing
Wikimedia sources are CC BY-SA and committed as snapshots. Visit Singapore's terms are restrictive, so
those would never be committed. **As built:** Visit Singapore proved to be entirely client-rendered
(17–106 characters of server-rendered text per page), so all three of its pages are recorded in the
registry as `unavailable` with the measured reason rather than dropped. Two extra Wikipedia articles
(Tourism in Singapore, Culture of Singapore) cover the attractions and cultural-guidance facets instead.
Delivered: **15 documents, ~523 KB, 2 publishers** — comfortably past the three-resource minimum.

---

## Target layout

```
NorthStar/
  .env.example
  requirements.txt             # grows task by task
  README.md                    # deliverable 18
  docs/
    ARCHITECTURE.md            # T0
    IMPLEMENTATION_PLAN.md     # this file
    PROMPT_STRATEGY.md         # brief §5
    SAMPLE_QA.md               # deliverable 19
    ACCEPTANCE.md              # brief §8 -> evidence
    DEMO.md                    # deliverable 20
  data/
    kb/
      sources.json             # source registry — authority on KB contents
      *.md                     # committed CC BY-SA snapshots + fetched/uploaded docs
    index/                     # FAISS + manifest.json (gitignored, rebuildable)
  app/
    config.py
    ingest/
      registry.py              # sources.json read/write
      fetch_sources.py         # curated source acquisition (CLI)
      parsers.py               # md/txt/html/pdf/docx -> markdown
      chunk.py                 # heading-aware split + category tagging
      build_index.py           # embed, persist, atomic swap
      service.py               # add/upload/preview/confirm/remove/rebuild + job state
    rag/
      retriever.py             # vector search, score floor, index reload
      kb_tool.py               # search_travel_knowledge_base tool
    mcp_servers/
      weather_server.py
      currency_server.py
    mcp_client.py
    llm.py
    prompts.py
    agent.py
    api.py                     # chat routes + app lifespan
    admin_api.py               # ingestion routes
    static/
      index.html               # chat UI
      admin.html               # ingestion UI
  scripts/
    smoke_*.py
```

---

## Tasks

### T0 — Architecture diagrams & design doc ✅
`docs/ARCHITECTURE.md`: system context, ingestion pipeline, ingestion UI + job lifecycle, query-time
sequence for the flagship combined scenario, tool-selection view, provenance & degradation model; plus
component table, data contracts, technology rationale, acceptance mapping, risks.
**Installs:** none.

### T1 — Scaffold, config, dependency workflow
Directory tree, `.gitignore` (`.venv/`, `data/index/`, `data/kb/visitsingapore_*`, `.env`, `__pycache__/`),
`.env.example`, empty `requirements.txt`, `.venv` via `uv venv`.
`app/config.py` with `pydantic-settings`: `llm_provider`, `groq_model`, `anthropic_model`, `kb_dir`,
`index_dir`, `embedding_model`, `retrieval_k`, `relevance_floor`, `destination` (`Singapore`),
`home_currency`, `max_upload_mb`, `allowed_upload_extensions`.
**Installs:** none (`pydantic-settings`, `python-dotenv`, `httpx`, `lxml`, `fastapi`, `uvicorn` already
exist in the interpreter; pin into the venv when a task first imports them).
**Verify:** `python -c "from app.config import settings; print(settings)"`.

### T2 — Source registry + KB acquisition
`app/ingest/registry.py` — load/save `data/kb/sources.json`, add/remove/update entries, per-source chunk
counts. This is the authority on KB contents from the start, because T13's UI manages the same file.
`app/ingest/fetch_sources.py` — seed the curated set. **As built:** Wikivoyage Singapore + 10 district
pages and 4 Wikipedia articles (MRT, Singaporean cuisine, Tourism in Singapore, Culture of Singapore),
all committed; 3 Visit Singapore pages attempted and recorded `unavailable`. Wikimedia via the REST HTML
endpoint; strip nav/edit/ref cruft but **keep Wikivoyage POI listings** (addresses, hours, prices);
per-source section filtering for the large Wikipedia articles; emit markdown that **preserves heading
hierarchy**.

Two things the environment forced, both documented in code so they do not get "fixed" back:
- **Transport is stdlib `urllib`, not `httpx`.** Wikimedia's bot protection answers httpx with 403
  regardless of User-Agent, Accept, Accept-Encoding, Connection or ALPN — it fingerprints below the HTTP
  layer. urllib is served normally. httpx arrives in T6, whose upstreams do no such filtering.
- **Throttle + `Retry-After` backoff.** A 0.4 s gap earned HTTP 429 part-way through the set; requests are
  now spaced 1.5 s apart with up to 4 retries honouring the server's `Retry-After`.
Each doc gets YAML frontmatter: `source_id`, `source_title`, `source_url`, `license`, `publisher`,
`origin: curated`, `retrieved_at`. Idempotent, prints a per-source status table, tolerates Visit Singapore
failure with a warning.
**Installs:** `beautifulsoup4`, `markdownify`.
**Verify:** `python -m app.ingest.fetch_sources`; every file has frontmatter and `##` headings;
`sources.json` lists all fetched sources with licences.

### T3 — Heading-aware chunking + category tagging
`app/ingest/chunk.py` — `MarkdownHeaderTextSplitter` on `#/##/###`, then `RecursiveCharacterTextSplitter`
(~900 chars, 120 overlap) for oversized sections. Chunk metadata: `source_id`, `source_title`,
`source_url`, `license`, `section_path` (`Singapore > Get around > MRT`), `chunk_id`, and a derived
`categories` list — `attractions`, `neighbourhoods`, `transport`, `culture`, `food`, `itinerary`, plus
**`indoor` / `outdoor`**. The indoor/outdoor tag is load-bearing: it drives the rainy-day swap in T11.
**Installs:** `langchain-text-splitters` (brings `langchain-core`).

**As built.** Chunking surfaced a content gap and two tagging gaps, all fixed:
- **The knowledge base had almost no itinerary content.** Wikivoyage's "Itineraries" section is only a
  *list of links* to separate articles, so `itinerary` matched 1 chunk out of 877. Added two Wikivoyage
  itinerary articles as sources (`Three days in Singapore`, `Southern Ridges Walk`) — `itinerary` now
  covers 50 chunks. This directly serves the brief's flagship three-day-itinerary scenario, so it was
  worth going back to T2 for.
- **Document lead sections were untagged** because they carry no H2 and the heading rules only read the
  section path ("Mass Rapid Transit" contains neither "MRT" nor "transport"). Added an explicit
  `SOURCE_CATEGORIES` map for curated documents whose whole subject is known; uploaded documents have no
  entry and fall back to heading/keyword rules.
- **Wikivoyage's fixed section names** "Respect", "Talk", "Learn", "Work" are exactly the cultural and
  practical tips the brief asks for, but matched no rule. Added them; untagged fell 43 → 1 chunk.

A low-signal filter for markdown-table chunks was **considered and rejected after measuring**: only 13 of
926 chunks are table-heavy, and the lowest letter-ratio chunks turned out to be embassy listings with
addresses and phone numbers — useful content a ratio filter would have discarded.

**Verify:** `python -m app.ingest.chunk --stats` prints chunk count, length distribution, per-source and
per-category counts; spot-check a transport chunk and an indoor-museum chunk.

### T4 — Embeddings + FAISS index + atomic swap
`app/ingest/build_index.py` — FastEmbed (`BAAI/bge-small-en-v1.5`, 384-dim) as a LangChain embeddings
object, `FAISS.from_documents(...)`, built into a **temp directory then atomically swapped** into
`data/index/`. Write `manifest.json` (model id, dim, chunk count, per-source counts, built_at) so the app
can detect a missing/stale index and fail with an actionable message. Update registry chunk counts.
Expose a callable `rebuild_index()` so T13 can drive it, not just the CLI.
**Installs:** `fastembed`, `faiss-cpu`, `langchain-community`.
**Verify:** `python -m app.ingest.build_index`; similarity search for "getting around Singapore by train"
returns MRT chunks with sensible scores; a deliberately failing build leaves the old index intact.

### T5 — Retriever + grounding guard + index reload
`app/rag/retriever.py` — singleton index holder with a `reload()` used after a rebuild.
`app/rag/kb_tool.py` — `search_travel_knowledge_base(query, categories=None, k=None)` as a LangChain
`@tool` over `similarity_search_with_relevance_scores`, optional `categories` filter, and a
**relevance floor**: if the best score is under `settings.relevance_floor`, return the sentinel
`NO_RELEVANT_CONTENT` plus the attempted query instead of weak chunks. Returns numbered excerpts prefixed
`[S1] <source_title> — <section_path> (<url>)` for citation-by-marker, plus a machine-readable source list
for the API layer.
**Installs:** none (`langchain-core` arrives with `langchain-community`).
**Verify:** `scripts/smoke_rag.py` over the brief's six §4.1 questions plus "What are the best ski resorts
in Singapore?", which must yield `NO_RELEVANT_CONTENT`.

### T6 — MCP server 1: weather
`app/mcp_servers/weather_server.py` — stdio `FastMCP` over **Open-Meteo** (free, keyless):
- `get_current_weather(city="Singapore")` → temp, humidity, condition text, precipitation, observed-at.
- `get_weather_forecast(city="Singapore", days=3, start_date=None)` → per-day min/max, precipitation
  probability and total, condition text, plus derived `outdoor_suitability` (`good|mixed|poor`) that T11
  keys the indoor/outdoor swap off.
Geocoding via Open-Meteo with Singapore as a hardcoded fallback; WMO codes mapped to readable text. Uniform
envelope `{ok, source, retrieved_at, data|error}` — network failures never produce a fabricated forecast.
`days` clamped to the API horizon, with an explicit message when the request exceeds it.
**Installs:** `mcp[cli]`.
**Verify:** `scripts/smoke_mcp_weather.py` (or `mcp dev`) calls both tools; point the base URL at an
unreachable host and confirm a clean `ok: false`.

### T7 — MCP server 2: currency
`app/mcp_servers/currency_server.py` — stdio `FastMCP` over **Frankfurter** (ECB rates, free, keyless):
- `convert_currency(amount, from_currency, to_currency)` → converted amount, rate, `rate_date`.
- `get_exchange_rate(from_currency, to_currency)`.
Validate ISO-4217 codes and reject unknown ones with a reason; surface `rate_date` so answers can state how
current the rate is. Same envelope.
**Installs:** none (reuses `mcp[cli]`, `httpx`).
**Verify:** `scripts/smoke_mcp_currency.py` — INR 50,000 → SGD, 200 SGD → INR, and `XYZ` → clean error.

### T8 — MCP client wiring
`app/mcp_client.py` — `MultiServerMCPClient` launching both servers as stdio subprocesses
(`sys.executable -m app.mcp_servers.<name>`), then `get_tools()` → LangChain tools. Connect once at
startup, close on shutdown. If a server fails to start: log it, **drop only that server's tools**, record
it in a `degraded_tools` list the prompt and API expose — the app must still serve RAG-only answers
(brief §4.2 item 14). Log discovered tool names/schemas at startup.
**Installs:** `langchain-mcp-adapters`.
**Verify:** `scripts/smoke_mcp_client.py` prints 4 tools with schemas and invokes one of each; rename a
server file and confirm degradation instead of a crash.

### T9 — LLM factory
`app/llm.py` — `get_chat_model()` returning a `BaseChatModel` per `settings.llm_provider`. Groq default;
the **exact Groq model id is resolved from the live model catalogue in this task** (`GET /openai/v1/models`,
pick a current tool-calling-capable model) rather than hardcoded from memory. Anthropic path via
`langchain-anthropic` with `claude-opus-5`. Fail fast naming the missing env var.
**Installs:** `langchain-groq` (plus `langchain-anthropic` only if that path is exercised).
**Verify:** `scripts/smoke_llm.py` does a trivial completion and a trivial tool-call round-trip, proving
tool calling works on the chosen model.

### T10 — Prompt engineering
`app/prompts.py` + `docs/PROMPT_STRATEGY.md`. System prompt encoding the brief §5 contract:
1. Destination facts come **only** from `search_travel_knowledge_base`, never from model priors.
2. Weather and currency come **only** from MCP tools, never estimated.
3. Do not call MCP tools for questions the KB already answers (brief §4.2 closing rule).
4. On `NO_RELEVANT_CONTENT`, state plainly what is missing and offer what is available.
5. On a tool error, state the failure and what is unavailable; never substitute a guess.
6. Fixed provenance vocabulary: **📚 From the knowledge base** (with `[S1]` citations),
   **🌐 Live via MCP — `<tool>` (retrieved `<ts>`)**, **💡 Suggestion**; closing **Sources** block.
7. Carry forward stated preferences (budget, family/kids, dietary, pace, dates) and restate the ones
   being applied.
**Installs:** none.
**Verify:** reviewed against T11 output.

### T11 — Agent assembly
`app/agent.py` — **`create_agent` from `langchain.agents`** (LangChain 1.x; confirm the signature against
the installed package first) over `[kb_tool, *mcp_tools]`, with a `langgraph` `InMemorySaver` checkpointer
keyed by `thread_id` = session id. The agent's own loop satisfies "appropriate tool selection based on
user intent" — no hand-rolled intent classifier. Add `extract_provenance(messages)` returning
`{kb_sources: [...], tool_calls: [...]}`. Expose `ask(session_id, question) -> (answer_markdown, provenance)`.
**Installs:** `langchain`, `langgraph`.
**Verify:** `scripts/smoke_agent.py` asserts which tools fire for:
1. "Must-visit attractions in Singapore?" → KB only, no MCP.
2. "What's the weather in Singapore right now?" → weather MCP only.
3. "Convert INR 50,000 to SGD." → currency MCP only.
4. "Three-day Singapore itinerary for next week, adjusted to the weather forecast." → KB **and** weather
   MCP, day-wise, with indoor alternatives on poor-weather days.
Then multi-turn: "I'm travelling with two young kids" → "now build me a 2-day plan" must stay
family-appropriate without the constraint being repeated.

### T12 — Document parsers
`app/ingest/parsers.py` — one entry point `parse_to_markdown(path_or_bytes, filename)` dispatching by
extension: `.md`/`.txt` passthrough, `.html` via `markdownify`, `.pdf` via `pypdf`, `.docx` via
`python-docx` (map Heading 1/2/3 styles to `#`/`##`/`###` so chunking still has a heading hierarchy).
Returns `{markdown, detected_headings, char_count, warnings}`. Reject empty or unreadable extractions with
a stated reason rather than producing an empty document.
**Installs:** `pypdf`, `python-docx`.
**Verify:** `scripts/smoke_parsers.py` over one sample of each format, including a heading-bearing DOCX and
a text-light PDF that must be rejected with a clear message.

### T13 — Ingestion service + job runner
`app/ingest/service.py`:
- `preview_url(url)` / `preview_upload(file)` → parse and return extracted markdown, detected headings and
  proposed metadata **without committing**.
- `confirm(preview_token, metadata)` → write `data/kb/<id>.md` with frontmatter, add the registry entry
  (`origin: url|upload`, licence required, defaulting to `user-supplied` for uploads), enqueue a rebuild.
- `remove_source(source_id)` → delete document, registry entry and chunks together.
- `rebuild()` → chunk → embed → build into temp → atomic swap → `retriever.reload()`.
- In-memory job store with `{job_id, kind, state, stage, progress, log, error}`; **one rebuild at a time** —
  a second request returns the in-flight `job_id`. A failed rebuild leaves the working index untouched.
**Installs:** none (reuses T3/T4/T12).
**Verify:** `scripts/smoke_ingest_service.py` — preview → confirm → rebuild an uploaded doc, query the
retriever and get a citation from it; then remove it and confirm it stops being retrievable; then force a
rebuild failure and confirm the previous index still answers.

### T14 — FastAPI backend
`app/api.py` (chat) + `app/admin_api.py` (ingestion), one app, one lifespan that starts the MCP client and
loads the index.
- `POST /chat {session_id, message}` → `{answer, kb_sources, tool_calls, degraded_tools}`
- `GET /health` → index manifest + MCP status · `GET /sources` → registry with licences · `POST /reset`
- `GET /admin/status` · `POST /admin/sources/url` · `POST /admin/sources/upload` ·
  `POST /admin/sources/confirm` · `DELETE /admin/sources/{id}` · `POST /admin/rebuild` ·
  `GET /admin/jobs/{id}`
Upload size/extension validated per `settings`. Errors return a useful message, not a stack trace.
**Installs:** `fastapi`, `uvicorn`, `python-multipart` (for uploads).
**Verify:** `uvicorn app.api:app --reload`; curl every endpoint; `/health` reports both MCP servers up and
the index present; an oversized upload is rejected with a clear 4xx.

### T15 — Chat UI
`app/static/index.html` — message list, input, session id in `localStorage`, "New conversation". Renders
answer markdown plus two collapsible panels per answer: **Knowledge-base sources** (title → clickable URL,
section path) and **MCP tool calls** (tool, args, timestamp, ok/failed). Banner when `degraded_tools` is
non-empty. Link to `/admin`. No build step; markdown via one CDN script.
**Installs:** none.
**Verify:** all four T11 scenarios in the browser show correct citations and tool badges; the degraded
banner appears when a server is broken.

### T16 — Ingestion UI (`/admin`)
`app/static/admin.html` — index status header (built_at, model, dim, chunk count, state), sources table
(title, licence, origin, chunk count, preview, remove), add-by-URL with **preview before commit**,
drag-and-drop upload for md/txt/html/pdf/docx with the same preview step, `Rebuild index` button, and a job
panel polling `GET /admin/jobs/{id}` with stage/progress/log. Disable destructive controls while a job runs.
**Installs:** none.
**Verify:** end-to-end in the browser — upload a PDF, see the preview, confirm, watch the rebuild job,
then ask the chat page a question that can only be answered from that PDF and get it cited. Remove it and
confirm the citation disappears.

### T17 — Failure-path hardening
Deliberately break each dependency and confirm honest degradation, not fabrication: missing FAISS index;
unreachable Open-Meteo; unreachable Frankfurter; invalid currency code; MCP server that won't launch;
missing/invalid LLM key; out-of-scope question; forecast beyond the API horizon; corrupt/scanned PDF upload;
rebuild failure mid-job; two concurrent rebuild requests. Fix anything that doesn't produce a clear message.
**Installs:** none.
**Verify:** the checklist above, each with observed output recorded for T18.

### T18 — `docs/SAMPLE_QA.md` (deliverable 19)
Verbatim transcripts from the running app: the six §4.1 KB questions, four §4.2 weather questions, three
§4.2 currency questions, the required §4.3 combined scenario, the three additional combined scenarios, a
3-turn preference-carrying conversation, and every failure case from T17. Each entry records the question,
the answer, which tools fired, and which sources were cited.
**Installs:** none.

### T19 — `README.md` (deliverable 18) + `docs/ACCEPTANCE.md`
README: architecture section built from the T0 diagrams (updated to what was actually built), KB source
table with licences and how to obtain the non-committed ones, the 7-step RAG workflow mapped to the brief's
numbered requirements, the ingestion UI walkthrough, MCP tool reference (server, tool, params, upstream
API), prompt & context strategy summary linking to `PROMPT_STRATEGY.md`, setup instructions, a "Python 3.14
notes" section, and a note that `/admin` is unauthenticated and local-only.
`ACCEPTANCE.md`: each of the brief's ten §8 criteria mapped to the file and transcript that evidences it.
**Installs:** none.

### T20 — Demo materials (deliverable 20)
`docs/DEMO.md` — a 5-minute run sheet: (1) `/admin` showing the knowledge base and uploading a document
live, (2) a KB-only question with citations, (3) a weather MCP call, (4) a currency MCP call, (5) the
combined weather-aware 3-day itinerary, (6) a follow-up turn proving retained context, (7) one failure case
showing honest degradation. Exact prompts to type and what to point at. Recording the video is the user's
step; this task delivers the script and verifies every beat works.
**Installs:** none.

---

## End-to-end verification

```powershell
uv venv; .\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
Copy-Item .env.example .env        # set GROQ_API_KEY
python -m app.ingest.fetch_sources
python -m app.ingest.build_index
python scripts\smoke_mcp_weather.py; python scripts\smoke_mcp_currency.py
python scripts\smoke_mcp_client.py
python scripts\smoke_ingest_service.py
python scripts\smoke_agent.py      # asserts tool selection for all 4 scenarios
uvicorn app.api:app --port 8000    # chat at /, ingestion at /admin
```

Acceptance is met when, in the browser: a KB question answers with clickable source links and **no** MCP
call; a weather question answers **only** from the MCP tool with a timestamp; a currency question returns a
rate with its ECB date; the three-day-itinerary prompt produces a day-wise plan citing both KB sources and
the weather tool, with indoor swaps on rainy days; a follow-up turn honours a preference stated two turns
earlier; an out-of-scope question states the knowledge base doesn't cover it; a document uploaded through
`/admin` becomes citable in chat after the rebuild; and killing a network dependency yields an explicit
"tool unavailable" rather than an invented number.

## Risks

1. **LangChain 1.x API drift** — 0.3-era `AgentExecutor`/`initialize_agent` patterns are gone. T11 reads
   the installed `langchain.agents` surface before writing code.
2. **Visit Singapore may block scripted fetches** or return a JS shell. The four Wikimedia sources already
   exceed the three-source minimum and a failed fetch is a warning.
3. **First FastEmbed run downloads ~130 MB** of ONNX model — needs network once, cached after.
4. **Groq model ids churn** — T9 resolves the id from the live catalogue.
5. **Forecast horizon** — "next week" near the edge must state how many days are actually available.
6. **Uploaded PDFs vary wildly** — no OCR in scope; mitigated by the mandatory preview and empty-extraction
   rejection in T12/T13.
7. **Full (not incremental) rebuilds** — fine at this scale; the job UI makes the wait visible.
8. **`/admin` is unauthenticated** — stated as local-only in the README rather than pretending otherwise.
