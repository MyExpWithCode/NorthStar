# NorthStar, from the ground up

This folder is a teaching copy of the ideas in `app/`. Nothing here imports
`app/`. Nothing here is used by the running application. You can delete the
whole folder and NorthStar still works.

The point: `app/` is written the way production code is written, which means
almost every interesting mechanism is hidden inside a library call. You cannot
learn what a vector store *is* by reading `FAISS.from_documents(...)`. So each
lesson below rebuilds one mechanism in plain Python, small enough to read in
one sitting, and only then shows you the library call that replaces it.

## How to run a lesson

Every lesson is a folder with a `README.md` and a `run.py`. Run it from the
project root with the project's own virtualenv:

```powershell
.venv\Scripts\python.exe learn\04-embeddings\run.py
```

```bash
# git bash
.venv/Scripts/python.exe learn/04-embeddings/run.py
```

Lessons reuse this project's `.venv`, its `.env` (for `GROQ_API_KEY`) and the
embedding model already downloaded into `.cache/fastembed`. No extra installs.
Nothing is written outside `learn/` except a scratch folder,
`learn/_scratch/`, which is gitignored.

## The order

Read them in order. Each one assumes the one before it.

| # | Lesson | The question it answers | Needs a key? |
|---|--------|------------------------|--------------|
| 00 | [The map](00-map/) | What are the 12 pieces, and which file is each one? | no |
| 01 | [Config](01-config/) | How does `.env` become typed Python? | no |
| 02 | [Fetching HTML](02-fetch-html/) | How does a web page become clean markdown? | no (network) |
| 03 | [Chunking](03-chunking/) | Why split documents, and where do you cut? | no |
| 04 | [Embeddings](04-embeddings/) | What *is* a vector, and why does similarity work? | no |
| 05 | [Vector store](05-vector-store/) | What does FAISS actually do that a for-loop doesn't? | no |
| 06 | [Retrieval](06-retrieval/) | How do you stop the model inventing facts? | no |
| 07 | [Calling an LLM](07-call-an-llm/) | What is underneath `ChatGroq(...)`? | **yes** |
| 08 | [Tool calling](08-tool-calling/) | How does a model "use a tool"? (it doesn't) | **yes** |
| 09 | [MCP](09-mcp-protocol/) | What is MCP really, on the wire? | no |
| 10 | [The agent loop](10-agent-loop/) | What is `create_agent`? (a while loop) | **yes** |
| 11 | [Memory](11-memory/) | How does it remember the last turn? | no |
| 12 | [Serving](12-serving/) | What does FastAPI add over `http.server`? | no |
| 13 | [The mini app](13-mini-app/) | All of it, one file, zero LangChain | **yes** |

Then: **[ALTERNATIVES.md](ALTERNATIVES.md)** — every technology choice in
NorthStar, what else exists, and when you would pick differently.
And **[GLOSSARY.md](GLOSSARY.md)** — the jargon, in one line each.

## The one-paragraph version

A travel assistant cannot be trusted to answer from memory, because language
models state wrong things confidently. So NorthStar answers only from evidence.
Evidence comes from two places: **a knowledge base** of travel documents we
downloaded and indexed ourselves (that is the RAG half), and **live tools** for
things a document cannot know, like today's weather and today's exchange rate
(that is the MCP half). A language model sits in the middle. Its job is not to
know things — it is to decide which evidence to fetch, then write prose over
what came back. Everything in `app/` is in service of that one sentence.

## What "no fancy things" means here

These lessons deliberately drop, in the name of clarity:

- error handling and retries
- caching, locking, singletons, atomic writes
- degraded-mode fallbacks
- type annotations beyond the useful ones
- configurability (values are hardcoded so you can see them)

`app/` has all of that, and needs it. When you go back to `app/` after these
lessons, most of what looks like complexity will turn out to be one of the five
things above wrapped around a mechanism you now recognise.
