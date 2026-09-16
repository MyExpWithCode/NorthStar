# Acceptance criteria — evidence

Each of the brief's ten minimum acceptance criteria (§8), with where it is implemented and how it was
verified. Every "verified by" entry is a script you can run.

| # | Criterion | Status |
|---|---|---|
| 1 | Knowledge base created from at least three travel resources | ✅ 17 documents, 2 publishers |
| 2 | Embedding-based semantic retrieval | ✅ FastEmbed + FAISS, 926 vectors |
| 3 | Grounded answers with source references | ✅ citations from tool artifacts |
| 4 | Weather information through an MCP tool | ✅ 2 tools over Open-Meteo |
| 5 | Currency conversion through an MCP tool | ✅ 2 tools over Frankfurter |
| 6 | At least one response combining RAG and MCP | ✅ 3 combined scenarios pass |
| 7 | Multi-turn conversation with retained context | ✅ 3 turns, 2 preferences retained |
| 8 | Appropriate tool selection based on user intent | ✅ 6/6 scenarios, asserted both ways |
| 9 | Clear handling of missing knowledge and tool failures | ✅ 26/26 failure paths |
| 10 | A simple, usable interface | ✅ chat + ingestion pages |

---

## 1. Knowledge base from at least three travel resources

**Delivered:** 17 documents, ~553,000 characters, across Wikivoyage and Wikipedia — comfortably past the
three-resource minimum. Source table in the [README](../README.md#knowledge-base-sources).

Each document retains its source title, URL, publisher, licence and retrieval date in YAML frontmatter, and
`data/kb/sources.json` records the same metadata as the authority for citations.

The brief also recommends Visit Singapore. It was attempted and **the failed attempt is recorded rather
than hidden**: all three pages are registered with `state: "unavailable"` and the measured reason (17 and
106 characters of server-rendered text — the site is entirely client-rendered). Visible in `/admin` and
`GET /sources`.

**Verified by:** `python -m app.ingest.fetch_sources` → per-source status table; `GET /sources`.

---

## 2. Embedding-based semantic retrieval

**Delivered:** FastEmbed ONNX `BAAI/bge-small-en-v1.5` (384-dim, local, keyless, no torch) over a FAISS
index of 926 chunks.

Relevance scores are **exact cosine similarities**, not squashed distances: vectors are unit-length and
compared by inner product with an identity relevance function. Verified against hand-computed cosine to
1e-4, which is what makes the relevance floor a number a human can reason about.

**Verified by:** `python scripts/smoke_rag.py` — all six of the brief's §4.1 questions retrieve grounded
content; the category filter used for wet-weather alternatives returns only correctly-tagged chunks.

---

## 3. Grounded answers with source references

**Delivered:** the knowledge-base tool returns numbered excerpts (`[S1]`, `[S2]`) with source title,
section path and relevance score. The UI renders a panel beneath each answer with the clickable source URL,
section path, relevance, licence and category tags.

**The citations come from the tool's artifact, not from parsing the answer text.** That is the point: a
citation cannot be fabricated by the model. If `[S1]` appears in an answer, a real retrieved chunk produced
it.

**Verified by:** `python scripts/smoke_api.py` — asserts `/chat` returns `kb_sources` with `marker`,
`title`, `url`, `section_path` and `score`. Transcripts in [SAMPLE_QA.md](SAMPLE_QA.md).

---

## 4. Weather information through an MCP tool

**Delivered:** `app/mcp_servers/weather_server.py`, a standalone stdio MCP server over Open-Meteo, exposing
`get_current_weather` and `get_weather_forecast`. It knows nothing about the RAG index, the agent or the
LLM — any MCP client can connect to it.

`outdoor_suitability` (`good`/`mixed`/`poor`) is derived **in the server** from the precipitation
probability, total and WMO code the service returned, rather than left for the model to infer.

**Verified by:** `python scripts/smoke_mcp_weather.py` — over a real stdio MCP session: tool discovery,
current conditions, a 3-day forecast, an over-horizon day count (clamped, with the clamp stated), a
far-future date (refused), a malformed date (refused), and an unknown city (fails cleanly rather than
inventing weather).

---

## 5. Currency conversion through an MCP tool

**Delivered:** `app/mcp_servers/currency_server.py` over Frankfurter (ECB reference rates), exposing
`convert_currency` and `get_exchange_rate`.

`rate_date` is always surfaced, because ECB rates publish once a business day — a Sunday conversion uses
Friday's rate, and saying so is more honest than implying a live quote.

**Verified by:** `python scripts/smoke_mcp_currency.py` — all three of the brief's currency examples
(INR 50,000 → SGD, 200 SGD → INR, USD budget → SGD) plus four failure paths: unknown code, non-code text,
negative amount, unknown target.

---

## 6. At least one response combining RAG and MCP

**Delivered:** the brief's required scenario works, plus two more.

The required one — *"Create a three-day Singapore itinerary for next week and adjust it according to the
weather forecast"* — produces this trace:

```
tools called : ['get_weather_forecast', 'search_travel_knowledge_base']
  - get_weather_forecast: ok | 3 day(s) for Singapore, 2 poor for outdoor activity
  - search_travel_knowledge_base: ok | 5 excerpt(s)
  - search_travel_knowledge_base: ok | 10 excerpt(s)
kb sources   : 15
```

Note the **two** knowledge-base searches. The agent fetched the forecast, saw that 2 of 3 days were poor
for outdoor activity, and went back to the knowledge base for indoor alternatives — the procedure the
prompt specifies, rather than inventing them. The forecast shapes *what gets retrieved*, which is the
substance of combining both sources rather than stapling them together.

**Verified by:** `python scripts/smoke_agent.py` scenarios 4 and 5. Transcripts in
[SAMPLE_QA.md](SAMPLE_QA.md).

---

## 7. Multi-turn conversation with retained context

**Delivered:** a LangGraph checkpointer keyed by `session_id`. Preferences stated once keep applying.

```
Q: I'm travelling to Singapore with two young children.
Q: We're on a tight budget too.
Q: Now build me a two-day plan.
   retained 'two young children' without restating: True
   retained 'tight budget' without restating      : True
   stored messages: 6 (3 user turns)
```

This exposed a real bug: retrieval excerpts accumulate in history, and the third turn was being rejected by
the provider as too large. Fixed with `ContextEditingMiddleware`, which clears tool results from earlier
turns while keeping the most recent so the current turn always retains its evidence.

A turn that fails provider-side still checkpoints the user's message, so nothing the user said is lost.

**Verified by:** `python scripts/smoke_agent.py --only-multiturn`; `GET /history/{session_id}`.

---

## 8. Appropriate tool selection based on user intent

**Delivered:** tool choice is made by the model from the tool descriptions and the system prompt. There is
deliberately **no hand-written intent classifier** — and one could not handle the flagship scenario, where
the *result* of the weather call determines whether a second knowledge-base search is needed.

Each scenario asserts which tools **must** fire and which **must not**, so a knowledge-base question that
triggers a weather call is a failure even if the answer reads well:

| Question | Tools called | Verdict |
|---|---|---|
| Must-visit attractions in Singapore? | `search_travel_knowledge_base` only | OK |
| What is the weather right now? | `get_current_weather` only | OK |
| Convert INR 50,000 to SGD | `convert_currency` only | OK |
| 3-day itinerary adjusted to the forecast | forecast + 2× KB search | OK |
| INR 60,000 budget + 3-day itinerary | `convert_currency` + KB search | OK |
| Best ski resorts in Singapore? | KB search, then admits the gap | OK |

The brief's rule that MCP must not answer questions the knowledge base covers is enforced by an explicit
prohibition in the prompt, not just a permission.

**Verified by:** `python scripts/smoke_agent.py` — 6/6.

---

## 9. Clear handling of missing knowledge and tool failures

**Delivered:** 26 failure paths, each producing an actionable statement and **no fabricated fact**.

The important finding here is that **a similarity threshold cannot implement "state when information is
unavailable"**. Measured: in-scope questions score 0.622–0.863, out-of-scope 0.485–0.713 — the ranges
overlap. "Best ski resorts in Singapore" scores 0.713, above a legitimate question about family activities
at 0.622.

So the guard is two layers: a calibrated relevance floor of 0.60 for the clearly-unrelated tail, and the
prompt — which receives each excerpt *with its score* and is told that a high score does not mean the
passage answers the question. That second layer is what correctly answers the ski-resort question:
Singapore has no ski resorts, only an indoor snow centre, which is both grounded and correct.

Three distinct sentinels drive three distinct responses: `NO_RELEVANT_CONTENT`,
`KNOWLEDGE_BASE_UNAVAILABLE`, and `"ok": false` on a tool result.

**Verified by:** `python scripts/smoke_failures.py` — 26/26. Evidence table in
[ARCHITECTURE.md §6.1](ARCHITECTURE.md).

---

## 10. A simple, usable interface

**Delivered:** two pages, one process, no build step.

**Chat (`/`)** — under every answer, two collapsible evidence panels: knowledge-base sources (marker,
clickable URL, section path, relevance, licence) and tool calls (tool, server, ok/failed, retrieved-at
timestamp, upstream service, arguments). A banner appears when a capability is degraded or no index has
been built.

**Ingestion (`/admin`)** — index status, a sources table with origin/licence/chunk counts, add-by-URL,
drag-and-drop upload for md/txt/html/pdf/docx, rebuild, and a live job log. Nothing is committed until a
preview is confirmed, and destructive controls are disabled while a rebuild runs.

**Verified by:** `python scripts/smoke_api.py` (all 12 endpoints through the real lifespan) and both pages
served from real uvicorn; inline JavaScript checked with `node --check`.

---

## Beyond the criteria

Things not required but built because the brief's spirit demanded them:

- **An ingestion UI**, so the RAG pipeline is inspectable rather than hidden behind scripts. A reviewer can
  upload their own document and watch it become citable in chat.
- **Atomic index swap.** A rebuild builds into a temp directory and swaps on success only, so chat keeps
  answering from the previous index during a rebuild and a failed rebuild cannot corrupt it. Verified by
  forcing a mid-write failure.
- **Per-server MCP degradation.** One server failing drops only its tools, names the missing capability in
  the prompt, and leaves the rest working.
- **Retrieval calibration as evidence**, not assertion — the score table in ARCHITECTURE.md §6.1 documents
  why the design is two-layered.
- **Scripts exit 2 when the LLM provider refuses**, so an exhausted free-tier quota is never mistaken for
  broken behaviour.
