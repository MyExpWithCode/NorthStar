# 13 — The mini app: all of it, one file, no LangChain

**Compare against:** the whole of [app/](../../app/) — 22 files, ~5,500 lines
**Dependencies used:** `httpx`, `fastembed`, `numpy` — and the standard library

**Needs `GROQ_API_KEY`.**

## What this is

`mini.py` is a working travel assistant. It has:

- a RAG knowledge base over the project's real 25 markdown documents
- embeddings and vector search
- the relevance floor and both refusal sentinels
- live weather and currency tools
- a tool-calling agent loop
- conversation memory
- citations with provenance
- an HTTP server and a chat UI

What it does **not** have:

- LangChain, LangGraph, langchain-core, langchain-community
- langchain-groq, langchain-mcp-adapters, langchain-text-splitters
- FAISS, MCP, FastAPI, uvicorn, pydantic, pydantic-settings
- BeautifulSoup, markdownify, pypdf, python-docx

**15 of the project's 19 dependencies, gone.** It runs in one process with no
subprocesses, and you can read the whole thing in one sitting.

```bash
# chat in the terminal
.venv/Scripts/python.exe learn/13-mini-app/mini.py

# one question and exit
.venv/Scripts/python.exe learn/13-mini-app/mini.py --ask "will it rain in Singapore?"

# the web UI
.venv/Scripts/python.exe learn/13-mini-app/mini.py --serve
# then open http://127.0.0.1:8799
```

## Why bother

Not to argue the real app is over-engineered. To answer a question the lessons
kept raising: **what was each dependency actually buying?**

You cannot answer that by reading `app/`, because the libraries are load-bearing
there. You answer it by removing them and seeing what breaks. Most of it
doesn't — and the few things that genuinely do are the honest case for each
library.

## The replacement table

| Real app | `mini.py` | Lines | Verdict |
|---|---|---|---|
| `pydantic-settings` | 10-line `.env` parser | 10 | fine at this size. No validation, no types |
| `langchain-text-splitters` | regex on `^#{1,3} ` + paragraph split | 30 | genuinely close for markdown |
| `faiss-cpu` | `VECTORS @ query` in numpy | 1 | **identical results.** Lesson 05 measured the speed |
| `langchain-community` FAISS wrapper | a python list of dicts | 0 | the wrapper is a docstore + id map |
| `langchain-groq` + `groq` SDK | `httpx.post` | 12 | need to set User-Agent yourself (lesson 07) |
| `langchain-core` `@tool` | hand-written JSON Schema | 60 | **most tedious loss.** Real work, and it drifts |
| `mcp` + `langchain-mcp-adapters` | plain functions calling `httpx` | 40 | loses interop, not capability |
| `langchain` `create_agent` + `langgraph` | a `while` loop | 25 | **the loop is easy. What you lose is below** |
| `langgraph-checkpoint-sqlite` | `dict[str, list]` + a JSON file | 15 | works; no time travel, no resume |
| `fastapi` + `uvicorn` | `ThreadingHTTPServer` | 70 | loses async, validation, OpenAPI |
| `fastembed` | **kept** | — | nothing sane replaces a 33M-param ONNX model |

Roughly **600 lines** replacing ~5,500. Most of the difference is not the
mechanisms — it is error handling, degraded modes, configurability, and the
`/admin` ingestion UI, which `mini.py` skips entirely.

## What genuinely got worse

This is the part worth reading twice. Six things:

**1. No `@tool` schema generation.** 60 lines of hand-written JSON Schema that
can silently drift out of sync with the functions. This was the single most
annoying thing to write, and lesson 08's demonstration was the most convincing
argument for any library in the project.

**2. No async.** `ThreadingHTTPServer` gives a thread per request instead.
Lesson 12 measured what the sync version costs: four concurrent requests took
4× as long as they needed to.

**3. No request validation.** `mini.py` checks `question` is a non-empty
string, by hand. FastAPI + pydantic gives field-level 422s for free.

**4. No time travel or resume.** The checkpointer stores state after *every
graph step*, which is what makes interrupt/resume and human-in-the-loop
possible. `mini.py` stores a message list per session. Fine for chat,
impossible to build approval-before-tool-runs on.

**5. No tracing.** LangSmith shows the whole agent loop for one turn — tool
choices, arguments, results. `mini.py` prints to stdout. For debugging *why
the model chose that tool*, this is a real loss.

**6. No interoperability.** The MCP servers work with Claude Desktop, Cursor
and any MCP client. `mini.py`'s functions work with `mini.py`.

## What turned out not to matter much

**FAISS.** Lesson 05 measured it: 0.08 ms vs 0.38 ms per query against a
~15 ms query embedding and a 1–3 s LLM call. At 1,298 chunks a numpy array
gives identical results. FAISS is a good default that costs nothing — but it is
not buying speed you can feel here.

**The `langchain-community` FAISS wrapper.** It is a docstore plus an
`index_to_docstore_id` dict. A list of dicts does the same thing.

**`pydantic-settings`, at this size.** 10 lines of parsing covers it. The
validation and the shadowing detection are real value in the full app; for one
file with hardcoded constants, less so.

## The honest conclusion

```
  KEEP (earn their place):
    fastembed            nothing else does this locally, keylessly
    langchain-core @tool schema generation from type hints
    fastapi + uvicorn    async + validation, both measured
    langgraph            ONLY for the checkpointer + middleware

  DEFENSIBLE EITHER WAY:
    faiss                free, scales, no observable benefit yet
    langchain-groq       one provider? use the SDK. Two? worth it
    mcp                  interop and isolation, not capability

  WOULD NOT MISS:
    langchain-community  a wrapper over a dict
```

**The most useful reframing** these lessons produced: LangChain is not one
dependency, it is five, and they have very different value here.
`langchain-core`'s `@tool` pays for itself immediately. `langgraph`'s
checkpointer pays for itself if you want persistent conversations.
`langchain-community` is a thin wrapper. Judge them separately.

**And the point of the exercise:** if you had built `mini.py` first, you would
have hit each wall yourself — the hand-written schemas drifting, the sync
server blocking, the context window filling — and adopted each library knowing
exactly which problem it solved. That is a much better place to be than
inheriting eighteen dependencies and guessing.

## Read it in this order

`mini.py` is sectioned to match the lessons:

```
  1. CONFIG            lesson 01
  2. KNOWLEDGE BASE    lessons 03-06   (chunk, embed, search, the floor)
  3. TOOLS             lessons 08-09   (weather, currency, KB search)
  4. THE LLM           lesson 07       (one httpx POST)
  5. THE AGENT         lesson 10       (the while loop)
  6. MEMORY            lesson 11       (a dict)
  7. THE SERVER        lesson 12       (ThreadingHTTPServer + HTML)
  8. CLI               --ask / --serve / interactive
```

Every section starts with a comment naming the lesson it came from and what it
replaced.

## Next

**[ALTERNATIVES.md](../ALTERNATIVES.md)** — every technology choice in the
project, what else exists, and when you would pick differently.
**[GLOSSARY.md](../GLOSSARY.md)** — the jargon, one line each.
