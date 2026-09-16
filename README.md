# NorthStar

> **Branch note.** This is `feat/multi-destination`. The knowledge base is
> **per-destination**: retrieval is scoped to the place a question is about, and
> a question about a place with no documents is refused with the covered list
> named, rather than answered from another place's guide. `main` holds the
> Singapore-only version.

A context-aware travel assistant that combines a **document-based RAG knowledge base** for stable
destination knowledge with **MCP tools** for live weather and currency information, and blends both in a
single answer.

The design rule everything follows from:

> **Every claim in an answer is attributable to exactly one of three origins** — the knowledge base (with
> source title and URL), an MCP tool (with tool name and retrieval timestamp), or the model's own
> suggestion (labelled as such). Nothing is asserted without an origin.

---

## Quick start

Requires Python 3.12+ (developed on 3.14.3) and a free [Groq](https://console.groq.com) API key.

```powershell
git clone <this repo>
cd NorthStar

uv venv                                  # or: python -m venv .venv
.\.venv\Scripts\Activate.ps1             # Linux/macOS: source .venv/bin/activate
uv pip install -r requirements.txt
uv pip install -e .                       # makes `app` importable from anywhere

Copy-Item .env.example .env               # then set GROQ_API_KEY in .env

python -m app.ingest.fetch_sources        # download the knowledge base (~2 min)
python -m app.ingest.build_index          # embed it (~1 min; first run downloads a 130 MB model)

uvicorn app.api:app --port 8000
```

Then open:

| URL | What it is |
|---|---|
| <http://localhost:8000/> | Chat, with a sources panel and a tool-call panel under every answer |
| <http://localhost:8000/admin> | Knowledge base: what is indexed, add/remove sources, rebuild |
| <http://localhost:8000/health> | Whether the index, the LLM and both MCP servers are actually up |

> **If `.env` seems to be ignored:** environment variables take precedence over the file. A machine-level
> `GROQ_API_KEY` will silently win. The app logs a warning naming any shadowed keys at startup and reports
> them under `dotenv_keys_shadowed_by_environment` on `/health`.

---

## Architecture

Full diagrams and design rationale: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

```
Browser                     Python process (uvicorn)                  External
┌──────────┐   POST /chat   ┌─────────────────────────────┐
│ chat  /  │ ─────────────► │ FastAPI                     │
└──────────┘ ◄───────────── │   └─ LangChain agent        │
   answer + provenance      │        ├─ tool: KB search ──┼──► FAISS index (in-process)
┌──────────┐                │        └─ MCP client ───────┼──► weather server ──► Open-Meteo
│ admin    │ ─────────────► │   └─ ingestion service      │    (stdio subprocess)
└──────────┘   uploads,     │        └─ chunk/embed/swap  │──► currency server ─► Frankfurter
               rebuild      └─────────────────────────────┘    (stdio subprocess)
                                        │
                                        └──► Groq (or Anthropic) for generation + tool choice
```

Boundaries worth knowing:

- **Knowledge-base search is in-process.** It is a vector search, not a network call — the point of RAG is
  that stable knowledge is answered locally and cheaply.
- **The two MCP servers are separate OS processes**, spoken to over stdio using the MCP protocol. They are
  the only components allowed to reach the live weather and currency services, and they know nothing about
  the RAG index, the agent or the LLM — any MCP client could connect to them.
- **The LLM never supplies facts.** It selects tools, composes prose, and makes labelled suggestions.
- **One process serves both pages**, so a rebuild started in `/admin` is immediately visible to chat.

---

## A note on the model

The default resolves to **`openai/gpt-oss-120b`**, which is confusing at a glance. To be explicit:

| | |
|---|---|
| Provider | **Groq** — every request goes to `api.groq.com` with `GROQ_API_KEY` |
| Model | **GPT-OSS 120B** — OpenAI's *open-weight* model (HF id `openai/gpt-oss-120b`), served on Groq |
| OpenAI involvement | **none** — there is no OpenAI credential in this project and no request reaches OpenAI |

Groq namespaces its catalogue by whoever published the weights, which is why the id carries an `openai/`
prefix, exactly as `qwen/qwen3.8-27b` is listed under Alibaba Cloud. Groq's endpoint path is also literally
`/openai/v1/` — that is its OpenAI-*compatible* API surface, not a call to OpenAI. `GET /health` reports
`llm.provider` alongside `llm.model` so this is unambiguous at runtime.

Set `LLM_PROVIDER=anthropic` with `ANTHROPIC_API_KEY` to use Claude instead.

## Observability (LangSmith)

Every agent run can be traced to LangSmith, and each answer in the UI carries an
**"Inspect this run in LangSmith"** link. That is worth having because the interesting question about
this app is never "what did it say" but **"which tools did it choose, what did they return, and does the
answer follow from that"** — a trace shows the whole loop: each tool call with its arguments and result,
the prompts as sent, and token counts.

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=ls__...
LANGSMITH_PROJECT=northstar
```

Both the switch and a key are required; the switch alone does nothing. `GET /health` reports
`tracing.enabled` and the project, and the header meter shows it, so you can tell at a glance whether a
run was captured. LangChain reads this configuration from the process environment rather than from our
settings object, so the app copies the values across at startup — otherwise putting keys in `.env` would
silently not enable tracing.

## Destinations

The knowledge base holds a **destination per document**, and retrieval is scoped to it. This matters for
honesty rather than tidiness: without scoping, "what should I see in Rome?" would retrieve Singapore
chunks and be answered confidently from the wrong city.

Shipped: **Singapore** (17 documents) and **Kyoto** (6 documents). Kyoto is there to demonstrate that the
pipeline is genuinely generic -- it was added with one command and no code change.

```powershell
# discover and fetch a destination's Wikivoyage guide and district pages
python -m app.ingest.fetch_sources --add-destination Tokyo
python -m app.ingest.fetch_sources --add-destination Tokyo --discover-only   # dry run
python -m app.ingest.build_index
```

Discovery uses the MediaWiki API: Wikivoyage names district guides as subpages (`Tokyo/Shinjuku`), so the
whole set for a city is found rather than hand-listed, with redirects skipped. A name with no Wikivoyage
article fails with a clear message instead of producing an empty destination.

Behaviour, verified in `scripts/smoke_rag.py`:

| Question about | Result |
|---|---|
| A covered place | scoped to that place; every citation is from it |
| An uncovered place | `DESTINATION_NOT_COVERED` -- refused, with the covered list named |
| No place given | searches all destinations; each excerpt is labelled with its place |

**Weather and currency are unaffected** -- they are live tools and work for any city. So the assistant can
tell you tomorrow's forecast in Reykjavik while saying plainly that it has no travel guide for it. The
prompt requires it to be explicit about which half it can help with.

To remove a destination: the `/admin` page has a "Remove all" button per destination, or
`DELETE /admin/destinations/{name}`.

## Knowledge-base sources

23 documents, ~750,000 characters, 1,271 chunks across two destinations. All committed to `data/kb/`, so a
fresh clone can build the index without network access to the sources.

| Source | Docs | Licence | Covers |
|---|---|---|---|
| [Wikivoyage: Singapore](https://en.wikivoyage.org/wiki/Singapore) + 10 district guides | 11 | CC BY-SA 4.0 | attractions, neighbourhoods, transport, food, practical tips |
| [Wikivoyage: Three days in Singapore](https://en.wikivoyage.org/wiki/Three_days_in_Singapore), [Southern Ridges Walk](https://en.wikivoyage.org/wiki/Southern_Ridges_Walk) | 2 | CC BY-SA 4.0 | day-by-day itineraries |
| [Wikipedia: Mass Rapid Transit (Singapore)](https://en.wikipedia.org/wiki/Mass_Rapid_Transit_(Singapore)) | 1 | CC BY-SA 4.0 | transport, fares, regulations |
| [Wikipedia: Singaporean cuisine](https://en.wikipedia.org/wiki/Singaporean_cuisine) | 1 | CC BY-SA 4.0 | food |
| [Wikipedia: Tourism in Singapore](https://en.wikipedia.org/wiki/Tourism_in_Singapore) | 1 | CC BY-SA 4.0 | attractions, landmarks |
| [Wikipedia: Culture of Singapore](https://en.wikipedia.org/wiki/Culture_of_Singapore) | 1 | CC BY-SA 4.0 | cultural guidance |
| Visit Singapore (3 pages) | **0** | restrictive | **attempted, unavailable — see below** |
| [Wikivoyage: Kyoto](https://en.wikivoyage.org/wiki/Kyoto) + 5 district guides | 6 | CC BY-SA 4.0 | second destination, added with `--add-destination` |

`data/kb/sources.json` is the authority on what is in the knowledge base: id, title, URL, publisher,
licence, origin, retrieval date and chunk count for each document.

### Why Visit Singapore is not in the knowledge base

The brief recommends it, so it was attempted and the attempt is recorded rather than hidden. Every page on
that site renders entirely on the client: a plain HTTP fetch of the itineraries page yields **17
characters** of text and the essential-information page **106**. Deep content pages are no better.
Extracting it would need a headless browser, which is out of proportion here and would not change the fact
that its terms do not permit redistribution.

All three pages are therefore registered with `state: "unavailable"` and the measured reason, visible in
`/admin` and `GET /sources`. Their facets are covered instead by *Tourism in Singapore* (attractions),
*Culture of Singapore* (cultural guidance) and the two Wikivoyage itinerary articles.

---

## RAG workflow

Mapped to the brief's numbered requirements:

| # | Requirement | Where | How |
|---|---|---|---|
| 1 | Load travel content | `app/ingest/fetch_sources.py` | Wikimedia REST HTML; nav/edit/reference chrome stripped, **POI listings deliberately kept** (addresses, hours, prices); per-source section filtering trims the 199 kB MRT article to what a traveller needs |
| 2 | Divide into meaningful chunks | `app/ingest/chunk.py` | Split on `#`/`##`/`###` first, then sub-split oversized sections on paragraph and list-item boundaries. 926 chunks, median 683 chars |
| 3 | Generate embeddings | `app/ingest/build_index.py` | FastEmbed ONNX `BAAI/bge-small-en-v1.5`, 384-dim — local, keyless, no torch |
| 4 | Store in a vector store | `app/ingest/build_index.py` | FAISS, built into a temp directory and **atomically swapped** so a failed rebuild leaves the working index serving |
| 5 | Retrieve relevant chunks | `app/rag/retriever.py` | Cosine similarity with a relevance floor and an optional category filter |
| 6 | Generate grounded answers | `app/prompts.py`, `app/agent.py` | The knowledge-base tool is the only permitted source of destination facts |
| 7 | Display the source | `app/rag/kb_tool.py`, `app/static/index.html` | `[S1]`-style markers in the answer; the panel beneath shows title, clickable URL, section path, relevance and licence |

**Every chunk is tagged at ingest** with `attractions`, `neighbourhoods`, `transport`, `culture`,
`practical`, `food`, `itinerary`, `shopping`, `accommodation`, and `indoor` / `outdoor` (0.1% untagged).
The indoor/outdoor tags are load-bearing: they are what lets the agent fetch *real* indoor alternatives
when the forecast says a day is wet.

### "Not in the knowledge base" is a two-layer guard

Measuring the retrieval scores showed that **a similarity threshold cannot do this job alone**: genuine
travel questions score 0.62–0.86 and unrelated ones 0.49–0.71, and **the ranges overlap**. "Best ski
resorts in Singapore" scores 0.713 — higher than a perfectly legitimate question about family activities
at 0.622 — because the embedding rewards the shared words.

So there are two layers:

1. **A relevance floor of 0.60** (calibrated from that measurement) rejects the clearly-unrelated tail.
   Below it the tool returns `NO_RELEVANT_CONTENT` and the model never sees weak chunks it might
   rationalise from.
2. **The prompt** handles the rest. Each excerpt is passed to the model *with its score*, and the prompt
   states that a high score does not mean the passage answers the question. This is the layer that
   correctly answers the ski-resort question — grounded *and* correct, since Singapore does have an indoor
   snow centre.

Evidence and the full score table: [ARCHITECTURE.md §6.1](docs/ARCHITECTURE.md).

---

## MCP tools

Two MCP servers, written for this project, launched as stdio subprocesses. Both upstream services are free
and **need no API key**, so there is no signup step and nothing to expire during a demo.

| Server | Tool | Parameters | Upstream |
|---|---|---|---|
| `weather` | `get_current_weather` | `city` | [Open-Meteo](https://open-meteo.com) |
| `weather` | `get_weather_forecast` | `city`, `days`, `start_date` | Open-Meteo |
| `currency` | `convert_currency` | `amount`, `from_currency`, `to_currency` | [Frankfurter](https://frankfurter.dev) (ECB rates) |
| `currency` | `get_exchange_rate` | `from_currency`, `to_currency` | Frankfurter |

Every tool returns the same envelope, so a failure is always reportable and never fabricable:

```json
{"ok": true,  "source": "Open-Meteo", "retrieved_at": "2026-09-16T05:23:33+00:00", "data": {...}}
{"ok": false, "source": "Open-Meteo", "retrieved_at": "...", "error": "Could not reach the weather service: ..."}
```

Design points:

- **`outdoor_suitability` (`good`/`mixed`/`poor`) is computed in the weather server** from the
  precipitation probability, total and WMO code the service actually returned — not left for the model to
  infer. It is the signal the weather-aware itinerary swap keys off.
- **`rate_date` is always surfaced.** ECB rates publish once a business day, so a Sunday conversion uses
  Friday's rate. Reporting the rate's own date lets the assistant say how current the number is instead of
  implying a live quote.
- **Unknown currency codes are rejected** against the service's own supported list, so `XYZ` produces a
  clear error rather than a plausible number. If that metadata endpoint hiccups, validation falls back to
  format-checking and lets the rate endpoint be the authority — a metadata outage must not block a
  conversion that would otherwise work.
- **Per-server degradation.** Each server connects independently. If one fails to start, only its tools
  drop, the failure is named in the system prompt so the model can say that capability is unavailable, and
  the app keeps serving everything else.

Run them standalone against any MCP client:

```
python -m app.mcp_servers.weather_server
python -m app.mcp_servers.currency_server
```

---

## Prompt and context strategy

Full reasoning: **[docs/PROMPT_STRATEGY.md](docs/PROMPT_STRATEGY.md)**. In brief:

- **Sourcing is phrased as an exclusive channel** — "the *only* permitted source of destination facts" —
  not a preference. The model's own Singapore knowledge is extensive and plausible, and supplementing from
  memory is the most likely way a wrong fact enters an answer.
- **The model's own reasoning is explicitly permitted**, but only when labelled `💡 Suggestion`. Forbidding
  judgement outright produces either a refusal or a covert violation; permitting it *with a label* makes
  the distinction enforceable.
- **Tool selection includes prohibitions**, because the brief requires that MCP not be used for questions
  the knowledge base already covers.
- **The indoor swap is procedural**: the prompt names the tool, the argument (`categories: ["indoor"]`) and
  the trigger. "Suggest indoor alternatives" would invite inventing them.
- **Three failure sentinels, three responses**: `NO_RELEVANT_CONTENT`, `KNOWLEDGE_BASE_UNAVAILABLE`, and
  `"ok": false` on a tool result. Each is a machine-checkable token, not a tone the model must infer.
- **Assembled per request, not a constant**, so today's date and the current tool availability are stated
  rather than assumed. A model that guesses the date silently mis-plans "next week".

**Context strategy.** A LangGraph checkpointer keyed by `session_id` holds the conversation. Retrieval
excerpts accumulate and are the bulk of every request, so `ContextEditingMiddleware` clears tool results
from earlier turns once context passes 4,000 tokens, keeping the three most recent so the current turn
always retains its evidence. Without this, the third turn of a conversation was rejected as too large.

---

## Verification

Each layer has a script that asserts behaviour, not just absence of crashes.

```powershell
python scripts/smoke_rag.py               # retrieval: the brief's 6 questions + out-of-scope + filters
python scripts/smoke_mcp_weather.py       # weather server over a real stdio MCP session
python scripts/smoke_mcp_currency.py      # currency server, incl. 4 failure paths
python scripts/smoke_mcp_client.py        # tool discovery + per-server degradation
python scripts/smoke_llm.py               # model resolution, completion, tool-call round trip
python scripts/smoke_parsers.py           # 5 upload formats + 5 rejection paths
python scripts/smoke_ingest_service.py    # upload -> cite -> remove, and rebuild safety
python scripts/smoke_api.py               # all 12 endpoints through the real lifespan
python scripts/smoke_failures.py          # 26 failure paths (cheap: one LLM call)
python scripts/smoke_agent.py             # tool selection per scenario + multi-turn (~10 min)
```

`smoke_agent.py` asserts **which tools must and must not fire** per scenario — a knowledge-base question
that triggers a weather call is a failure even if the answer reads well.

Scripts exit `0` on success, `1` on a real failure, and **`2` when the LLM provider refused the request**
so an exhausted free-tier quota is never mistaken for broken behaviour.

### Groq free-tier limits

The free tier allows **8,000 tokens per minute** and **200,000 per day**, shared across all chat models.
This workload is retrieval-heavy and a multi-tool turn can spend most of a minute's budget on its own, so:

- request payloads were cut ~38% (5 excerpts max, 520 chars each, no duplicated section path or per-excerpt
  URL in the model payload, and a tightened system prompt);
- `scripts/smoke_agent.py` paces itself and takes about 10 minutes for a full run;
- `scripts/generate_sample_qa.py` caches every answer and is resumable, so hitting the cap costs time
  rather than transcripts.

Set `LLM_PROVIDER=anthropic` with `ANTHROPIC_API_KEY` to avoid these limits entirely.

---

## Deliverables map

| Deliverable | Where |
|---|---|
| Source code | this repository |
| Working application | `uvicorn app.api:app` — chat at `/`, ingestion at `/admin` |
| Knowledge-base documents | `data/kb/` (committed) + `python -m app.ingest.fetch_sources` |
| README: architecture, sources, RAG workflow, MCP tools, prompts, setup | this file + [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/PROMPT_STRATEGY.md](docs/PROMPT_STRATEGY.md) |
| Sample questions and responses | [docs/SAMPLE_QA.md](docs/SAMPLE_QA.md) — verbatim, generated from real runs |
| Demonstration | [docs/DEMO.md](docs/DEMO.md) — run sheet |
| Acceptance criteria | [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) — criterion → evidence |
| Implementation history | [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) |

---

## Project layout

```
app/
  config.py            typed settings; paths derived, keys as SecretStr
  prompts.py           the grounding / provenance / preference contract
  llm.py               provider factory; Groq model resolved from the live catalogue
  agent.py             tool loop, memory, provenance extraction
  api.py               12 endpoints + both pages
  mcp_client.py        launches the MCP servers, per-server degradation
  ingest/
    registry.py        sources.json -- the authority on KB contents
    fetch_sources.py   curated source acquisition
    parsers.py         md / txt / html / pdf / docx -> markdown
    chunk.py           heading-aware split + category tagging
    build_index.py     embed, persist, atomic swap
    service.py         preview / confirm / remove / rebuild + job runner
  rag/
    retriever.py       vector search, relevance floor, index reload
    kb_tool.py         the knowledge-base tool given to the agent
  mcp_servers/
    weather_server.py  MCP server 1 (Open-Meteo)
    currency_server.py MCP server 2 (Frankfurter)
  static/              chat page, admin page
data/kb/               17 documents + sources.json   (committed)
data/index/            FAISS index + manifest.json   (gitignored, rebuildable)
docs/                  architecture, prompt strategy, sample Q&A, demo, acceptance
scripts/               one verification script per layer
```

---

## Notes and limitations

- **`/admin` is unauthenticated** and intended for local use only. It can delete knowledge-base documents
  and trigger rebuilds. Do not expose it.
- **No OCR.** A scanned PDF has no text layer and is rejected at upload with that reason rather than
  indexed as an empty document.
- **Rebuilds are full, not incremental.** Fine at this scale (seconds to embed 926 chunks); the job panel
  makes the wait visible.
- **FAISS loads with `allow_dangerous_deserialization=True`** because its docstore is a pickle. This is
  safe here for a specific reason: the only index ever loaded is one this application built itself, on this
  machine, from `data/kb/`.
- **Python 3.14 note.** The knowledge-base fetcher uses stdlib `urllib`, not `httpx`, because Wikimedia's
  bot protection answers httpx with HTTP 403 regardless of User-Agent, Accept, Accept-Encoding, Connection
  or ALPN settings — it fingerprints below the HTTP layer. The MCP servers still use httpx, whose upstreams
  do no such filtering. There is a comment in the code so this is not "modernised" back.
- **MCP SDK version.** `mcp` is pinned to 1.x: `langchain-mcp-adapters` requires `mcp<2`, and mcp 2.x
  renamed `FastMCP` to `MCPServer`. Client and server must agree on the major version.

## Licence and attribution

Application code in this repository is provided for assessment. Knowledge-base documents in `data/kb/` are
derived from Wikivoyage and Wikipedia and are licensed **CC BY-SA 4.0**; each file retains its source
title, URL, publisher and retrieval date in YAML frontmatter, and `data/kb/sources.json` records the same
metadata for citation.
