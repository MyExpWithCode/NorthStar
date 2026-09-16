# Glossary

The jargon, one or two lines each, in the meaning it has *in this project*.
Grouped by area rather than alphabetically, because the terms only make sense
next to their neighbours. Lesson links go to where the thing is demonstrated.

---

## Retrieval and RAG

**RAG** — Retrieval Augmented Generation. Three words for *I looked it up and
pasted it into the prompt*. You search your own documents with the user's
question and paste the top results in with an instruction to answer only from
them. [Lesson 00](00-map/)

**Embedding** — a fixed-length list of numbers representing a piece of text.
Here: 384 floats. Texts about similar things get similar numbers. Not a
language model — it cannot produce words at all. [Lesson 04](04-embeddings/)

**Vector** — the embedding, thought of as a point in 384-dimensional space.

**Cosine similarity** — the angle between two vectors, as a number from -1 to
1. Higher means more similar meaning. With unit-length vectors it is just the
dot product. [Lesson 04](04-embeddings/)

**Dot product** — multiply two vectors element-wise and sum. `sum(a[i]*b[i])`.

**Unit-length / L2-normalised** — a vector whose length is exactly 1.0. bge
models emit these, which is why cosine collapses to the dot product and
`RELEVANCE_FLOOR=0.60` is an interpretable number.

**Semantic search** — searching by meaning rather than by matching words.
"Cheap hawker food" finds "affordable local eateries" despite sharing no words.

**Chunk** — one retrievable piece of a document, ~900 characters here. You
search chunks, not documents, because one embedding for a 188 KB document is a
blurry average of everything it mentions. [Lesson 03](03-chunking/)

**Chunk overlap** — consecutive chunks sharing their last/first N characters,
so a fact near a boundary appears whole in at least one chunk. 120/900 = 13%
here.

**Section path** — `Wikivoyage: Singapore/Bugis > Eat > Budget`. The
`h1 > h2 > h3` trail, produced free by heading-aware splitting. Makes citations
readable, and is prepended before embedding so the vector contains those words.

**Relevance floor** — the cosine-similarity threshold (0.60) below which the KB
tool returns `NO_RELEVANT_CONTENT` instead of weak chunks. Deliberately coarse:
the score distributions for good and bad questions overlap.
[Lesson 06](06-retrieval/)

**Vector store / vector database** — something that stores vectors and finds
the nearest ones fast. At this scale a numpy array does it in one line.
[Lesson 05](05-vector-store/)

**FAISS** — Facebook AI Similarity Search. A C++ library for vector search.
Here configured as `IndexFlatIP`: exact, no approximation, inner product.

**ANN** — Approximate Nearest Neighbour. Accepting that you might occasionally
miss the true best match in exchange for speed. The reason FAISS exists, and
deliberately unused here — at 1,298 vectors it would trade correctness for
microseconds.

**HNSW** — Hierarchical Navigable Small World. A popular ANN index structure: a
graph you navigate toward the nearest neighbour.

**IVF** — Inverted File index. Cluster the vectors, then search only the
nearest clusters. Another ANN approach.

**Recall** — the fraction of true nearest neighbours an approximate search
actually returns. `IndexFlatIP` has 100% by construction.

**k / top-k** — how many results to return. `RETRIEVAL_K=5` here, capped hard
at `MAX_RETRIEVAL_K=5` because unbounded k blows the context window.

**`fetch_k` / over-fetching** — retrieving more candidates than you need
(`k*6`) so you still have enough after filtering in Python. A workaround for
FAISS's inability to filter — and the source of a measured bug.
[Lesson 05](05-vector-store/)

**BM25** — a classic keyword-ranking algorithm based on term frequency and
rarity. Not semantic; excellent at exact names and rare terms.

**TF-IDF** — Term Frequency × Inverse Document Frequency. BM25's simpler
ancestor. Rare words in a document count for more.

**Hybrid search** — run BM25 and vector search, merge the rankings. The
standard production answer.

**RRF** — Reciprocal Rank Fusion. The usual way to merge two ranked lists:
score each result by `1/(k + rank)` in each list and add.

**Reranking / cross-encoder** — over-fetch 50 results, then re-score them with
a slower model that reads the query and document *together*. Usually the
biggest single quality gain available.

**Query rewriting / expansion** — have the model turn `"where should I eat"`
into `"where to eat in Kyoto, restaurants and local food"` before searching.
Directly fixes the short-query failure measured in lesson 06.

**HyDE** — Hypothetical Document Embeddings. Generate a fake *answer*, embed
that, and search with it — because an answer looks more like a document than a
question does.

**Grounding** — constraining the model to answer only from supplied evidence.
The entire point of this app.

**Provenance** — the record of what an answer was built from. Here it comes
from the tool's *artifact*, not by parsing the answer text, so a citation
cannot be invented. [Lesson 08](08-tool-calling/)

**Artifact** — the second return value of a `content_and_artifact` tool. Goes
to your code only, never into the prompt. Carries URLs, licences and scores.

**Hallucination** — the model stating something false with confidence. The
failure this whole app is shaped around.

**MTEB** — Massive Text Embedding Benchmark. The leaderboard to check when
choosing an embedding model. Look at scores, not dimension counts.

---

## Models and APIs

**LLM** — Large Language Model. Text in, text out. Stateless.

**Token** — the unit a model reads and bills in. Roughly ¾ of an English word,
or ~4 characters. 900 characters ≈ 200–280 tokens.

**Tokeniser** — the thing that splits text into tokens. Model-specific.

**Context window** — the maximum tokens one request may contain. Exceed it and
you get HTTP 413.

**Prompt tokens / completion tokens** — what you sent vs what came back. You
are billed for both, at different rates.

**Reasoning tokens** — hidden thinking tokens some models emit before the
answer. You pay for them and never see them; `gpt-oss-120b` spent 51 of 69
completion tokens on reasoning for a 42-character answer.
[Lesson 07](07-call-an-llm/)

**Temperature** — randomness. 0 is near-deterministic; higher is more varied.
This app uses 0, because run-to-run variation in reported facts is a liability.

**System prompt** — standing instructions sent with every request. Re-sent on
**every iteration** of the agent loop, which is why `app/prompts.py` is
deliberately short.

**Message roles** — `system` (your instructions), `user` (the person),
`assistant` (the model), `tool` (your reply to a tool call).

**Stateless** — the API remembers nothing between requests. Any apparent memory
is you re-sending the history. [Lesson 07](07-call-an-llm/)

**`finish_reason`** — why the model stopped. `stop` means it wrote an answer;
`tool_calls` means it wants a function run.

**Prompt caching** — providers caching the unchanging prefix of your prompt so
repeat requests are cheaper. Makes requests cheaper, **not smaller** — no help
against a context limit.

**OpenAI-compatible API** — a provider offering the same request/response shape
as OpenAI's. Groq's `/openai/v1/` path is this; nothing is sent to OpenAI.

**GPT-OSS** — OpenAI's open-*weight* models. `openai/gpt-oss-120b` is served by
Groq and namespaced by who published the weights.

**Rate limit / 429** — too many requests or tokens per minute. Groq's free tier
meters tokens per minute; `max_retries=5` turns it into a slower answer.

**ONNX** — Open Neural Network Exchange. A portable model format with a fast
CPU runtime. How `fastembed` runs bge models without PyTorch.

---

## Tools and agents

**Tool / function calling** — the model replies with JSON naming a function and
its arguments; **your code** runs it and sends the result back. The model never
executes anything. [Lesson 08](08-tool-calling/)

**JSON Schema** — the format describing a function's parameters. The
`description` fields are prompt engineering — they are all the model knows
about your function.

**`tool_call_id`** — the id linking a tool request to your reply. Must
round-trip or the provider rejects the next request.

**Parallel tool calls** — several tool requests in one reply. Each needs its own
tool message.

**Agent** — a loop: while the model asks for a tool, run it and ask again.
~25 lines. [Lesson 10](10-agent-loop/)

**Agent loop / ReAct loop** — that loop. Named after the 2022 "Reasoning +
Acting" paper.

**ReAct prompting** — the pre-tool-calling technique of prompting
`Thought: ... Action: ...` and parsing the text. Brittle and obsolete; what old
LangChain `AgentExecutor` tutorials are doing.

**Intent classifier / router** — code that decides which tool to use from
keywords. Deliberately absent here, because the flagship scenario needs a
decision that depends on a tool's *result*.

**MCP** — Model Context Protocol. JSON-RPC 2.0 over stdio or HTTP for exposing
tools to LLM applications. A distribution and interoperability standard, not a
better way to call a function. [Lesson 09](09-mcp-protocol/)

**JSON-RPC 2.0** — a minimal RPC format: `{"jsonrpc":"2.0","id":1,"method":...,
"params":...}`. A message with an `id` is a request; one without is a
notification, and expects no reply.

**stdio transport** — the MCP server runs as a subprocess and speaks over
stdin/stdout. **Consequence: never `print()`** — stdout is the protocol channel.

**FastMCP** — the decorator-based MCP server API (`@server.tool()`). Generates
schemas from type hints, like LangChain's `@tool`.

**Degraded mode** — running with some capabilities missing rather than
refusing to start. Here: one MCP server down leaves the others working, and the
system prompt is told what is unavailable so the model says so rather than
guessing.

**Envelope** — the `{ok, source, retrieved_at, data|error}` shape both MCP
servers return. Makes a failure *data* the model can report.

**Human-in-the-loop** — pausing before a tool runs for approval. Needs
resumable state, which is what LangGraph's checkpointer provides and a `while`
loop cannot.

---

## Frameworks

**LangChain** — not one library but five. Worth judging separately:
`langchain-core` (messages, `@tool`, `Document`), `langchain` (`create_agent`),
`langchain-community` (integrations, being sunset), `langchain-groq` (a provider
client), `langchain-mcp-adapters` (MCP → LangChain tools).

**LangGraph** — expresses the agent loop as a state machine. The point is not
the loop but that state becomes *data*: snapshottable, resumable, inspectable.

**`create_agent`** — a preassembled LangGraph tool-calling loop.

**Node / edge** — the graph's steps and the transitions between them. This
agent has two nodes: `model` and `tools`.

**Checkpointer** — saves graph state after every step, keyed by `thread_id`.
What makes conversations persistent. [Lesson 11](11-memory/)

**`thread_id`** — the conversation key. Here, the session id.

**Middleware** — code inserted at defined points in the graph.
`ContextEditingMiddleware` is the only one used.

**`ClearToolUsesEdit`** — the middleware that replaces old tool results with a
placeholder once history passes 4,000 tokens, keeping the 3 most recent.

**Time travel** — addressing any past checkpoint. Free consequence of saving
state per step.

**LangSmith** — LangChain's tracing service. Records each run's tool choices,
arguments and results.

**msgpack** — a compact binary serialisation format. What LangGraph stores
checkpoints in — notably safer than pickle, which is what `index.pkl` uses.

**Pickle** — Python's native serialisation. Loading an untrusted pickle
**executes code**, which is why `FAISS.load_local` needs
`allow_dangerous_deserialization=True`.

---

## Web and Python

**ASGI** — Asynchronous Server Gateway Interface. The async successor to WSGI;
what FastAPI speaks and uvicorn serves.

**WSGI** — the older synchronous standard (Flask, Django).

**`async` / `await`** — Python's concurrency for I/O-bound work. `await` yields
the event loop while waiting, so one thread serves many slow requests.
Lesson 12 measured 4× on four concurrent requests.

**Event loop** — the scheduler running coroutines.

**Coroutine** — what an `async def` returns. Does nothing until awaited.

**`asyncio.TaskGroup` / anyio task group** — runs concurrent tasks together.
Relevant because failures arrive wrapped in an `ExceptionGroup`, which is why
`mcp_client.py` unwraps them to get a readable error.

**`ExceptionGroup`** — several exceptions raised as one. `ExceptionGroup:
unhandled errors in a TaskGroup (1 sub-exception)` is true and useless; unwrap
to the real cause.

**Lifespan** — FastAPI's startup/shutdown hook. Where the agent is built once
rather than per request.

**`AsyncExitStack`** — holds async context managers open beyond the function
that created them. Used so the SQLite connection outlives
`_make_checkpointer()`.

**pydantic** — declarative validation from type hints. Appears three times
here: settings (lesson 01), tool schemas (lesson 08), HTTP bodies (lesson 12).
Learn it once.

**`SecretStr`** — a pydantic type that prints as `**********`, so a key cannot
leak into a log by accident. You must call `.get_secret_value()`.

**OpenAPI** — the machine-readable API spec FastAPI generates from your type
annotations, and the source of `/docs`.

**422 vs 400** — 422 Unprocessable Entity is what FastAPI returns for a
well-formed request that fails validation, with the field name and the rule
that failed.

**`User-Agent`** — the HTTP header identifying your client. Python's default is
rejected with **403** by both Wikimedia and Groq's edge. A real thing client
libraries set for you. [Lessons 02](02-fetch-html/), [07](07-call-an-llm/)

**ATX vs setext headings** — `## Heading` vs an underline of `---`. ATX is
required here, because the markdown splitter looks for `#` characters.

**Frontmatter** — the `---` metadata block at the top of each knowledge-base
document: source URL, publisher, licence, `retrieved_at`. Where citations come
from.
