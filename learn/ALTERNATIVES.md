# Every choice in NorthStar, and what else exists

One table per decision. The **Verdict** column is my honest read after writing
the lessons and measuring things, not a general ranking. Where a lesson
measured something, it says so.

---

## 1. Configuration

**Used:** `pydantic-settings` · **Lesson:** [01](01-config/)

| Option | `.env` | Types | Validation | Pick it when |
|---|---|---|---|---|
| `os.environ` | no | no | no | one script, two settings |
| `python-dotenv` | yes | no | no | you want `.env` loading only |
| **`pydantic-settings`** | yes | yes | yes | **default for an app** |
| `environs` | yes | yes | some | lighter, same idea |
| `dynaconf` | yes | yes | some | layered dev/stage/prod files |
| Hydra / OmegaConf | via yaml | yes | some | ML experiments, CLI overrides |
| a plain `config.py` | n/a | yes | yes | no secrets, no per-env change |

**Verdict: keep.** The cross-field validator (`chunk_overlap < chunk_size`) and
the `.env`-shadowing detection are the parts that justify it — neither is
something a hand-rolled parser ever gets around to. Also: pydantic appears
three times in this codebase (config, tool schemas, HTTP bodies), so learning
it once pays off repeatedly.

---

## 2. Fetching and cleaning HTML

**Used:** `urllib` + `beautifulsoup4` + `markdownify` · **Lesson:** [02](02-fetch-html/)

### Downloading
| Option | Sync/async | Notes |
|---|---|---|
| **`urllib.request`** | sync | **used by the fetcher.** stdlib, zero deps, clumsy API |
| `requests` | sync | the friendly classic; not async |
| **`httpx`** | both | **used by the MCP servers.** requests-like + async + HTTP/2 |
| `aiohttp` | async | older async standard, heavier |
| `scrapy` | framework | crawling thousands of pages with queues and politeness |

**Verdict: correct as-is, and note the pattern.** `requirements.txt` explicitly
documents why the fetcher uses `urllib` while the MCP servers use `httpx`: *a
dependency should be justified by the requirement at the site where it is used*.
The fetcher runs once, offline, sequentially. The servers are async on the
user's critical path.

### Extracting content
| Option | Approach | Pick it when |
|---|---|---|
| **`beautifulsoup4` + `markdownify`** | you list what to delete | **used here.** Full control, needs per-site selectors |
| `trafilatura` | heuristic main-content extraction | **arbitrary user-supplied URLs** |
| `readability-lxml` | the Firefox Reader algorithm | same idea, older |
| `html2text` | HTML → markdown | markdownify alternative |
| `docling` / `unstructured` | multi-format → structured | heavyweight; good PDF tables |
| Firecrawl / Jina Reader | hosted URL → markdown | zero code, per-page cost |
| Playwright / Selenium | real browser | **JavaScript-rendered pages only** |

**Verdict: right for a curated corpus, wrong for arbitrary URLs — and the
project does both.** Lesson 03 found the consequence: a URL added through
`/admin` goes through `parsers.py`'s 7 selectors instead of
`fetch_sources.py`'s 32, and Wikipedia's navigation sidebar got indexed as
travel content. **Fixing this is the highest-value change available**, because
lesson 06 showed those chunks are effectively unreachable.

### PDF
| Option | Notes |
|---|---|
| **`pypdf`** | **used here.** Pure python, text only, loses headings |
| `pdfplumber` | better layout and tables |
| `PyMuPDF` | fastest and best quality; **AGPL — check before shipping** |
| `docling` / `unstructured` | can recover heading structure |
| AWS Textract / Azure DI | OCR for scans, per-page cost |

**Verdict: acceptable, with a known limit.** A PDF has no `<h2>`, so uploaded
PDFs produce chunks with no section path and degrade retrieval. `parsers.py`
refuses documents under 200 extracted characters rather than indexing filler,
which is the right instinct.

---

## 3. Chunking

**Used:** `langchain-text-splitters` · **Lesson:** [03](03-chunking/)

| Strategy | Cost | Pick it when |
|---|---|---|
| fixed-size characters | free | never, for prose |
| `RecursiveCharacterTextSplitter` | free | **sane default** for unstructured text |
| **`MarkdownHeaderTextSplitter`** | free | **used here.** Markdown with real headings |
| `HTMLHeaderTextSplitter` | free | skip the markdown conversion |
| token-based (`tiktoken`) | free | you must hit an exact token budget |
| code splitters (AST) | free | indexing source code |
| **semantic chunking** | 1 embedding/sentence | **prose with no headings** |
| propositional / LLM chunking | 1 LLM call/doc | highest quality, highest cost |
| parent-document retrieval | more storage | precise matching, wide context |
| contextual retrieval | 1 LLM call/chunk | prepend an LLM summary to each chunk |
| late chunking | long-context embedder | keeps global context per chunk |

**Verdict: close to optimal for this corpus.** Lesson 03 measured it — the
naive splitter cut 13 of 20 boundaries mid-word; the two-stage splitter cut
zero and produced a readable section path for free. That is a fact about
*curated Wikivoyage markdown*, not a general truth.

**Worth replacing `mini.py`-style if:** your corpus has no headings.

---

## 4. Embeddings

**Used:** `fastembed` + `BAAI/bge-small-en-v1.5` · **Lesson:** [04](04-embeddings/)

### Libraries
| Option | Runtime | Install | GPU | Fine-tune |
|---|---|---|---|---|
| **`fastembed`** | ONNX | ~50 MB | no | no |
| `sentence-transformers` | PyTorch | ~2.5 GB | yes | yes |
| `transformers` directly | PyTorch | ~2.5 GB | yes | yes |
| an API (OpenAI/Cohere/Voyage) | remote | tiny | n/a | some |

### Models
| Model | Dims | Local | Notes |
|---|---|---|---|
| **`bge-small-en-v1.5`** | 384 | yes | **used here.** Best size/quality for English |
| `bge-base-en-v1.5` | 768 | yes | ~3× slower, modestly better |
| `all-MiniLM-L6-v2` | 384 | yes | the old default; bge-small beats it |
| `nomic-embed-text-v1.5` | 768 | yes | 8k context, good for long chunks |
| `multilingual-e5` / `bge-m3` | 1024 | yes | **pick if your corpus is not English** |
| OpenAI `text-embedding-3-small` | 1536 | no | strong, cheap, needs key + network |
| Voyage `voyage-3` | 1024 | no | top of several leaderboards |
| **BM25 / TF-IDF** | n/a | yes | **not semantic** — exact terms. Still excellent |

**Verdict: keep, clearly.** `fastembed`'s docstring reason — *no torch* — is
the whole argument: 50 MB vs 2.5 GB for a model that embeds 1,300 chunks once
and one short query per search, on CPU. Invert that trade the moment you need
to fine-tune.

**Two measured corrections from lesson 04:**

1. **Dimensions are not quality.** 1536 dims is 4× the storage and search cost,
   not 4× better. Check MTEB scores.
2. **The usual "embeddings fail on exact names" warning did not hold here.**
   Five proper nouns (`Tian Tian Hainanese Chicken Rice`, `EZ-Link card`,
   `Kinkaku-ji`, `Haw Par Villa`, `Fushimi Inari`) each ranked a literally
   matching chunk at #1. Measure retrieval claims on your own data.

**The limit that does bite:** embeddings cannot hear negation.
`"open on Monday"` vs `"closed on Monday"` = **0.882**. And `"indoor
activities"` is closer to its own antonym (0.779) than to `"museums and
galleries"` (0.692). That is the concrete justification for the `categories`
filter being hard metadata rather than a similarity search.

---

## 5. Vector store

**Used:** `faiss-cpu` + `langchain-community` · **Lesson:** [05](05-vector-store/)

| Store | Type | Runs | Filtering | Pick it when |
|---|---|---|---|---|
| **numpy array** | exact | in-process | in python | **< ~50k vectors.** Genuinely fine |
| **FAISS** | exact or ANN | in-process | **no** | **used here.** Fast, local, no infra |
| `sqlite-vec` | exact/ANN | a file | SQL | already using SQLite |
| **Chroma** | ANN | embedded/server | **yes** | easiest "real" vector DB |
| **Qdrant** | HNSW | server | **yes, excellent** | filtering + hybrid search |
| **pgvector** | exact/ANN | PostgreSQL | **SQL** | **already run Postgres → start here** |
| Weaviate | ANN | server | yes | built-in hybrid search |
| Milvus | ANN | cluster | yes | billions of vectors |
| Pinecone | ANN | cloud | yes | fully managed, per-query cost |
| Elasticsearch | both | cluster | yes | BM25 + vectors, already deployed |
| LanceDB | ANN | files | yes | FAISS-like with filtering + versioning |

**Verdict: defensible, but the one place I would actually change something.**

Lesson 05 measured FAISS at 0.08 ms/query vs numpy's 0.38 ms — against a
~15 ms query embedding and a 1–3 s LLM call. At 1,298 chunks FAISS buys no
speed you can feel. It is a fine default because it costs nothing and scales;
the ANN index types that justify its existence (`IndexIVFFlat`, `IndexHNSW`)
are deliberately unused here, correctly, since approximation at this size is
pure loss.

**The real limit is filtering, not speed.** `IndexFlatIP` cannot filter, so
`retriever.py` over-fetches `k*6` and filters in Python. Measured failure:
`destination=Kyoto` + `query="where should I eat"` → **20 chunks clear the
relevance floor index-wide, only 3 survive the top-30 window**, because
Singapore is 71% of the index. `fetch_k = k*12` recovers all five. Qdrant,
Chroma and pgvector filter inside the index and cannot have this bug.

**The second limit:** adding one document rebuilds all 1,298 vectors.

**Also:** `index.pkl` is a pickle, so loading an untrusted one executes code —
which is why `FAISS.load_local` requires
`allow_dangerous_deserialization=True`. Contrast with LangGraph's checkpoints
(lesson 11), which are msgpack: data only.

---

## 6. Retrieval quality

**Used:** a fixed relevance floor · **Lesson:** [06](06-retrieval/)

| Technique | Fixes | Cost |
|---|---|---|
| **fixed threshold** | garbage results | free. **used here** |
| top-k, no threshold | nothing | free; always returns something — dangerous here |
| **query rewriting / expansion** | short, vague queries | 1 LLM call |
| HyDE | vocabulary mismatch | generate a fake answer, embed that |
| **hybrid search (BM25 + vector)** | exact names, rare terms | ~2× search cost |
| **cross-encoder reranking** | ranking precision | over-fetch 50, rerank |
| LLM-as-judge filter | marginal results | 1 LLM call per chunk |
| RAG-Fusion / multi-query | one phrasing missing results | N queries + RRF |
| self-RAG / CRAG | knowing when to retrieve | the model critiques its own retrieval |

**Verdict on the floor: correctly implemented and honestly documented.** It is
calibrated by measurement, and `config.py` calls it *"deliberately a COARSE
guard"* because the distributions overlap — which lesson 06 confirmed. At the
shipped 0.60, `"what is the capital of Peru"` scores 0.621 and gets through.
Showing the score to the model and letting it judge is the right compensation.

**Ranked by value for this codebase, on the lessons' evidence:**

1. **Fix the `url-contents` ingestion path** (lesson 03) — bad content is
   unreachable content, and no query-time tuning rescues it
2. **Query rewriting** (lesson 06) — directly fixes the measured short-query
   failure. Cheap: even a template helps
3. **Reranking** — reliably improves ranking; cheap to add
4. **Raise `fetch_k`** from `k*6` to `k*12` (lesson 05) — one character
5. Hybrid search — robustness on rare terms; no observable failure yet

**One subtlety lesson 06 found that is worth knowing before you tune
anything:** because `chunk.py` prepends the section path before embedding, a
query containing the destination name lifts ~95% of that destination's chunks
above the floor. So the floor's protective value depends on query phrasing —
an interaction between two files that never mention each other. *Measure
exhaustively before adjusting a threshold.*

---

## 7. The LLM

**Used:** Groq via `langchain-groq` · **Lesson:** [07](07-call-an-llm/)

### Providers
| Provider | Strength | Watch out for |
|---|---|---|
| **Groq** | very fast, free tier | **used here.** Small catalogue, **aggressive rate limits** |
| **Anthropic** | strongest reasoning + tool use | no free tier |
| OpenAI | ecosystem, reliability | cost |
| Google Gemini | huge context, cheap | different API shape |
| Together / Fireworks | many open models | quality varies |
| **Ollama** | **local, free, private** | needs a decent GPU |
| vLLM / TGI | self-hosted at scale | you operate it |
| Bedrock / Azure OpenAI | enterprise compliance | more setup |

### Client layers
| Layer | Pick it when |
|---|---|
| raw HTTP | zero deps, one provider — **but set a User-Agent or get 403** |
| provider SDK | **one provider, done properly.** Often the right answer |
| **LangChain** | **used here.** Multiple providers, or you want the ecosystem |
| LiteLLM | 100+ providers behind one interface, nothing else |
| Instructor / Outlines | you mainly want guaranteed structured output |
| Pydantic AI | typed agents, lighter than LangChain |
| DSPy | optimise prompts programmatically |

**Verdict: `langchain-groq` is justified by provider-swapping, not by the chat
call.** `app/llm.py` supports Groq and Anthropic in ~40 lines because
`ChatGroq` → `ChatAnthropic` changes nothing else. With one provider, the
`groq` SDK would be simpler.

**Two things `app/llm.py` gets notably right:**

- **Model resolution from the live catalogue.** The catalogue currently holds
  13 models and **not one `llama-3.3-*` id** — so every tutorial naming one is
  already broken. A hardcoded id is a time bomb.
- **Excluding `groq/compound`.** Those are agentic systems with their own
  built-in tools; mixing them in would make it impossible to say which tool
  produced which fact, breaking the provenance guarantee.

**Practical note on Groq's free tier:** it is metered per minute and writing
these lessons repeatedly hit HTTP 429 with `Retry-After: 51s` and up to 196s.
`max_retries=5` is not a precaution.

---

## 8. Tool definition

**Used:** `langchain-core` `@tool` · **Lesson:** [08](08-tool-calling/)

| Approach | Notes |
|---|---|
| hand-written JSON Schema | full control, tedious, **drifts from the code** |
| **LangChain `@tool`** | **used here.** Schema from type hints + docstring |
| pydantic model as schema | more precise validation |
| `mcp` `@mcp.tool()` | same idea, exposed over a protocol |
| provider-native helpers | e.g. OpenAI's `pydantic_function_tool` |

**Verdict: the clearest keep in the project.** Writing `mini.py` required 60
lines of hand-written JSON Schema that can silently drift out of sync with the
functions. This was the most convincing argument for any library here.

**And the counter-intuitive design lesson from `kb_tool.py`:** its `categories`
argument is deliberately *not* a `Literal` enum, because Groq validates enums
server-side and the model inventing `"family"` **400'd the entire request** and
killed the chat turn. The fix is to accept `list[str]`, validate inside the
tool, drop unknowns, and tell the model what was ignored. **Strict validation
turned a recoverable mistake into a fatal error.** Be lenient with a
non-deterministic caller that can read your error messages.

---

## 9. Tool transport

**Used:** MCP over stdio · **Lesson:** [09](09-mcp-protocol/)

| Approach | Interop | Isolation | Complexity |
|---|---|---|---|
| plain python functions | none | none | **lowest.** See `mini.py` |
| LangChain `@tool` | LangChain only | none | low |
| **MCP** | **any MCP client** | **process** | medium. **used here** |
| OpenAPI + a spec bridge | any HTTP client | network | medium |
| gRPC | any gRPC client | network | higher |
| vendor plugins/actions | that vendor | network | vendor-locked |

**Verdict: defensible either way, and worth being clear about why.**

**MCP is a distribution and interoperability standard, not a better way to call
a function.** Making these plain functions would delete two subprocesses, the
JSON-RPC layer, `langchain-mcp-adapters`, `parse_tool_payload`'s three-shape
problem and the degraded-mode dance — about 150 lines. `mini.py` does that and
works fine.

What MCP buys pays off *outside* this process: the weather server works with
Claude Desktop, Cursor or any MCP client unchanged; a hang cannot take the web
app down; and — the real payoff — speaking MCP means you can adopt any of the
hundreds of existing MCP servers (filesystem, Postgres, GitHub, Slack) with no
integration work.

**Two things the servers do that are worth stealing regardless of transport:**

- **The `{ok, source, retrieved_at, data|error}` envelope.** A failure is
  *data*, not an exception, so "the forecast is unavailable" is a sentence the
  assistant can actually say.
- **`outdoor_suitability` is computed by the server.** That turns a judgement
  call ("is 24 mm of rain a problem?") into a fact the model can act on — and
  it is what makes the flagship scenario work.

**Version trap:** `mcp[cli]==1.30.0` is pinned because `langchain-mcp-adapters`
requires `mcp<2`, and mcp 2.x renamed `FastMCP` to `MCPServer`. MCP is young;
the client libraries lag the spec.

---

## 10. The agent loop

**Used:** `langchain.create_agent` + `langgraph` · **Lesson:** [10](10-agent-loop/)

| Approach | Lines | Gets you |
|---|---|---|
| **hand-written `while` loop** | ~40 | full control, no deps. **`mini.py`** |
| loop + provider SDK | ~60 | + retries, typed responses |
| **`create_agent`** | ~10 | **used here.** Checkpointing, middleware, streaming, interrupts |
| hand-built LangGraph | ~50 | the same, for non-linear flows |
| Pydantic AI | ~15 | typed, lighter, less ecosystem |
| OpenAI Agents SDK | ~15 | good handoffs/guardrails; OpenAI-centric |
| CrewAI / AutoGen | ~30 | **multi-agent.** Overkill for one agent |
| `smolagents` | ~20 | agent writes python instead of JSON calls |
| DSPy | varies | optimises the prompt rather than the loop |

**Verdict: `langgraph` earns its place through the checkpointer and middleware,
not the loop.** The loop is 25 lines (`mini.py` proves it). What you cannot
retrofit onto a `while` loop is *resumable, inspectable state* — that is
genuinely LangGraph, and it is what makes persistent conversations,
interrupt/resume and `ContextEditingMiddleware` possible.

**The design decision most worth studying** is the absence of a hand-written
intent router. The flagship scenario — *"will it rain Thursday, and what should
I do if it does?"* — requires a second KB search **conditional on the forecast
result**. That decision is not in the question; it is in data that did not
exist when the question was asked. A keyword router cannot express it. *That is
the actual reason agents exist: conditional multi-step work where later steps
depend on earlier results.*

---

## 11. Conversation memory

**Used:** `langgraph-checkpoint-sqlite` · **Lesson:** [11](11-memory/)

### Storage
| Store | Survives restart | Concurrent | Notes |
|---|---|---|---|
| a python list | no | no | fine for a script. **`mini.py`** |
| `InMemorySaver` | no | per-process | **used by the smoke scripts** |
| **`AsyncSqliteSaver`** | yes | **single-writer** | **used here.** One file, zero ops |
| `PostgresSaver` | yes | yes | the multi-instance answer |
| Redis checkpointer | yes | yes | fast, TTL-able, less durable |
| your own table | yes | yes | most control |

**Verdict: right for one process, and the migration trigger is concurrency —
not file size.** SQLite is single-writer, so two app instances on one file hit
`database is locked`. That, not growth, is when you move to `PostgresSaver`
(and the application code does not change, which is one real thing the
abstraction buys).

**The awkward part lesson 11 measured:** the checkpointer is a *state store*,
not a conversation index. It writes a checkpoint after every **graph step** —
16 for one conversation here — so `list_conversations` scans `limit * 12`
checkpoints and groups in Python, about **30× more rows than needed**. The fix
is a boring `(session_id, title, updated_at, turn_count)` table of your own.

### Context control
| Technique | Cost | Trade-off |
|---|---|---|
| **clear old tool results** | free | **used here.** Placeholder says to re-search |
| sliding window | free | forgets the start of the conversation |
| LLM summarisation | 1 call | keeps gist, loses detail, adds latency |
| token-count trimming | free | needs a tokeniser |
| vector-retrieve past turns | 1 embedding/turn | scales far, more complex |
| provider prompt caching | cheaper, **not smaller** | does not help a hard context limit |

**Verdict: well targeted.** Lesson 11 measured retrieval excerpts at ~80% of
the payload by turn 3, and they are re-fetchable — exactly the right thing to
drop. Note `trigger=4000` vs the library default of 100,000: **a default tuned
for a paid tier is actively wrong on a free one**, and 100,000 would never fire
before Groq returned HTTP 413.

**Also note what kind of memory this is not.** NorthStar has conversation
history only — no summarisation, no semantic long-term memory, no user profile,
no cross-session recall. When someone says "add memory" they usually mean one
of those four, and none of them is a checkpointer.

---

## 12. HTTP serving

**Used:** `fastapi` + `uvicorn` · **Lesson:** [12](12-serving/)

| Framework | Async | Validation | Pick it when |
|---|---|---|---|
| `http.server` | no | no | learning, or a 20-line utility |
| Flask | no | manual | simple sync apps, huge ecosystem |
| **FastAPI** | **yes** | **pydantic** | **used here.** The default for a JSON API |
| Starlette | yes | no | FastAPI without pydantic; lighter |
| Litestar | yes | pydantic/attrs | FastAPI-like, opinionated |
| Django + DRF | partial | serialisers | you want ORM + admin + auth |

**Verdict: keep, and lesson 12 measured why.** Four concurrent 1.5 s requests
took **6.01 s** on single-threaded `http.server` (the fourth user waits for the
first three) and **1.51 s** on FastAPI + uvicorn in one thread, because `await`
yields the event loop. For an app whose requests are dominated by waiting on an
LLM, that is the right model — it is why `api.py`'s routes are `async def`.
Validation and the free OpenAPI schema are a bonus.

**Deployment trap:** `gunicorn -w 4` means four processes, each building its own
agent (4 embedding models, 8 MCP subprocesses) and all opening the **same
single-writer SQLite file**. Use one worker with many async connections, or
`PostgresSaver` plus a shared index.

**Honestly absent:** auth, rate limiting, CORS, request ids, and **streaming**.
Token streaming (SSE or WebSocket) is the biggest *perceived* performance win
available, since answers take seconds.

---

## 13. Observability

**Used:** LangSmith (optional)

| Option | Notes |
|---|---|
| **LangSmith** | **used here.** Zero-config with LangChain; SaaS |
| Langfuse | open source, self-hostable, similar features |
| Phoenix (Arize) | open source, strong evaluation tooling |
| Weights & Biases Weave | if you already use W&B |
| **OpenTelemetry** | vendor-neutral; pairs with existing infra |
| plain structured logging | free; you build the trace view |

**Verdict: genuinely well-chosen for this app.** The interesting question about
an agent is never "what did it say" but *"which tools did it choose and what
did they return"* — and `app/observability.py` attaches the trace URL to each
answer so a reviewer can inspect the whole loop rather than trusting the
provenance panel. The one weakness is that it is SaaS-only; Langfuse is the
self-hosted equivalent.

---

## The summary I would give someone starting over

**Keep — each earns its place, and a lesson shows why:**

| Dependency | Because |
|---|---|
| `fastembed` | nothing else embeds locally and keylessly without 2.5 GB of torch |
| `langchain-core` (`@tool`) | 60 lines of hand-written, drift-prone JSON Schema |
| `fastapi` + `uvicorn` | measured 4× concurrency win, plus free validation |
| `langgraph` | **only** for the checkpointer and middleware |
| `pydantic-settings` | cross-field validation and the shadowing trap |
| `langchain-text-splitters` | measurably better cuts on this corpus |

**Defensible either way:**

| Dependency | The trade |
|---|---|
| `faiss-cpu` | free and scales, but no measurable benefit at 1,298 chunks — and it is the *reason* for the over-fetch filtering bug |
| `langchain-groq` | worth it for two providers; use the SDK for one |
| `mcp` + adapters | interoperability and isolation, not capability |

**Would not miss:** `langchain-community` — a docstore plus an id map.

**The most useful reframing:** *LangChain is not one dependency, it is five,
and they have very different value here.* Judge `langchain-core`,
`langchain-community`, `langgraph`, `langchain-groq` and
`langchain-mcp-adapters` separately.

**And the real recommendation:** build `mini.py` first. You will hit each wall
yourself — schemas drifting, the sync server blocking, the context window
filling, HTTP 429 — and adopt each library knowing exactly which problem it
solved. That is a much better place to be than inheriting eighteen dependencies
and guessing.
