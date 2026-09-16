# 00 — The map

Before any code: what are the pieces, and what is each one *for*.

## The data flows twice

NorthStar has two completely separate flows. Confusing them is the single most
common reason this kind of app is hard to understand.

### Flow A — ingestion (happens rarely, offline)

```
  a URL or an uploaded file
        |
        v
  [1] download it                      urllib  (app/ingest/fetch_sources.py)
        |
        v
  [2] strip nav/ads, convert to        BeautifulSoup + markdownify
      markdown                                 (app/ingest/parsers.py)
        |
        v
  [3] cut into ~900-char chunks        langchain-text-splitters
      at heading boundaries                    (app/ingest/chunk.py)
        |
        v
  [4] turn each chunk into 384         fastembed (ONNX)
      numbers                                  (app/ingest/build_index.py)
        |
        v
  [5] store the numbers so they        faiss-cpu
      can be searched fast                     (data/index/index.faiss)
```

Run once. Output: a folder of files on disk. No LLM is involved anywhere in
Flow A — **an embedding model is not a language model.**

### Flow B — a chat turn (happens on every question)

```
  "will it rain in Singapore on Thursday, and what should I do if it does?"
        |
        v
  [6]  HTTP POST /chat                 FastAPI + uvicorn   (app/api.py)
        |
        v
  [7]  load conversation history       langgraph checkpointer (SQLite)
        |
        v
  [8]  send history + tool list        langchain-groq -> api.groq.com
       to the model                              (app/llm.py)
        |
        v
  [9]  model replies: "call            <- this is the whole trick
       get_weather_forecast"
        |
        v
  [10] run that tool                   MCP over stdio    (app/mcp_servers/)
        |
        v
  [11] send the result back to the     ...loop back to [8]
       model; it may call another
       tool (e.g. search the KB
       for `indoor` activities)
        |
        v
  [12] model writes prose, we          app/agent.py
       attach provenance                        extract_provenance()
        |
        v
  the answer + a list of what it was built from
```

Steps [8]–[11] repeat until the model stops asking for tools. That loop **is**
the agent. Everything marketed as an "agent framework" is a wrapper around it.

## Every technology, one line each

Grouped by what it is *for*, because that is what gets lost.

### Getting text in
| Tool | Job | Lesson |
|---|---|---|
| `urllib` (stdlib) | download a URL | 02 |
| `beautifulsoup4` | parse HTML, delete nav/ads/scripts | 02 |
| `markdownify` | HTML -> markdown, so headings survive | 02 |
| `pypdf`, `python-docx` | read uploaded PDFs / Word files | 02 |

### Making text searchable
| Tool | Job | Lesson |
|---|---|---|
| `langchain-text-splitters` | cut documents at heading + paragraph boundaries | 03 |
| `fastembed` | text -> 384 floats, locally, no API key | 04 |
| `faiss-cpu` | store thousands of vectors, find the nearest fast | 05 |

### Talking to the model
| Tool | Job | Lesson |
|---|---|---|
| `langchain-groq` | HTTP client for api.groq.com, in LangChain's shape | 07 |
| `langchain-core` | the shared vocabulary: `Message`, `Document`, `@tool` | 07, 08 |

### Giving the model things to do
| Tool | Job | Lesson |
|---|---|---|
| `mcp[cli]` (FastMCP) | run weather/currency as standalone tool servers | 09 |
| `httpx` | those servers call Open-Meteo / Frankfurter over HTTPS | 09 |
| `langchain-mcp-adapters` | translate MCP tools into LangChain tools | 09 |

### Running the loop
| Tool | Job | Lesson |
|---|---|---|
| `langgraph` | the tool-calling loop as a state machine | 10 |
| `langchain` (`create_agent`) | a preassembled LangGraph loop | 10 |
| `langgraph-checkpoint-sqlite` | save conversation state to a `.sqlite3` file | 11 |

### Surrounding it
| Tool | Job | Lesson |
|---|---|---|
| `fastapi` + `uvicorn` | HTTP routes, JSON validation, static files | 12 |
| `pydantic-settings` | `.env` -> a typed, validated settings object | 01 |
| `python-multipart` | lets FastAPI accept file uploads | 12 |
| LangSmith | records each run so you can see tool choices | 10 |

**Lesson 13 rebuilds the working app using three of these** (`httpx`,
`fastembed`, `numpy`). That is not an argument that the rest are waste — it is
how you find out what each one was actually buying.

## The three ideas that matter

Strip the libraries away and NorthStar is three ideas.

**1. RAG = search, then paste.** There is no magic. You search your own
documents using the user's question, take the top few results, and paste them
into the prompt with an instruction that says "answer only from this".
"Retrieval Augmented Generation" is three words for *I looked it up and pasted
it in*.

**2. Tool calling = the model returns JSON, you run the function.** The model
cannot call anything. It has no network and no filesystem. You send it a list
of function signatures; it replies with a name and arguments as JSON; **your
code** runs the function and sends the result back as another message. The
model never touches a tool. Lesson 08.

**3. An agent = a while loop.** `while the model asked for a tool: run it, tell
it the result`. That is all. Lesson 10 writes it in about 40 lines.

## Why this app looks harder than those three ideas

Nearly all remaining code in `app/` exists for one of four reasons. Learning to
spot them is most of learning to read the codebase:

- **Refusing to guess.** The relevance floor, `NO_RELEVANT_CONTENT`,
  `DESTINATION_NOT_COVERED`, the degraded-tool prompt note. All of it exists so
  that "I don't know" is a reachable outcome. (Lesson 06.)
- **Provenance.** Citations come from the tool's *artifact*, not from parsing
  the model's text, so a citation cannot be fabricated. (Lesson 08.)
- **Token budgets.** `EXCERPT_CHAR_LIMIT`, `MAX_RETRIEVAL_K`,
  `ContextEditingMiddleware`. Groq's free tier allows a few thousand tokens per
  minute and retrieval excerpts blow through that by turn three. (Lesson 11.)
- **Not dying.** Per-server MCP connection, the index atomic swap, provider
  fallbacks, `.env` shadowing detection.

## Run it

```bash
.venv/Scripts/python.exe learn/00-map/run.py
```

That prints the diagram above with this project's real numbers attached: how
many documents, how many chunks, how big the index is, how many conversations
are stored.

## Next

[01 — Config](../01-config/) — the smallest piece, and a gentle start.
