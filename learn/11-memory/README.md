# 11 — Memory: there isn't any

**Real files:** [app/agent.py](../../app/agent.py) (`history`,
`list_conversations`, `reset`), [app/api.py](../../app/api.py)
(`_make_checkpointer`)
**Libraries:** `langgraph`, `langgraph-checkpoint-sqlite`

No API key needed — this lesson inspects the real `conversations.sqlite3`.

## Start from lesson 07's fact

**The model is stateless.** It remembers nothing between requests. So
"conversation memory" is not a model feature — it is entirely your problem:

> **Memory = you kept the message list and re-sent it.**

That is the whole concept. Everything else is storage engineering.

## Level 1 — a python list

```python
messages = [system]
while True:
    messages.append({"role": "user", "content": input()})
    reply = call_model(messages)
    messages.append(reply)
```

Works perfectly. Two problems:

1. **Dies with the process.** Restart the server, lose the trip you were
   planning.
2. **Grows forever.** This is the interesting one.

## The growth problem, with real numbers

Every turn re-sends everything. So cost per turn is not constant — it is
*cumulative*:

```
  turn 1:  system + Q1                                    ~1,200 tokens
  turn 2:  system + Q1 + A1 + Q2                          ~2,600 tokens
  turn 3:  system + Q1 + A1 + Q2 + A2 + Q3                ~4,300 tokens
```

And with retrieval in the loop it is much worse, because a single turn may hold
**5 excerpts × 520 chars × several searches**. Retrieval excerpts dominate
history — `run.py` measures the real split.

`app/agent.py` records what actually happened:

> Retrieval excerpts accumulate in history and are the bulk of every request.
> Left alone, **the third turn of a conversation gets rejected as too large.**

Not a theoretical concern. HTTP 413.

## Level 2 — trim it

```python
ContextEditingMiddleware(edits=[ClearToolUsesEdit(
    trigger=4000,          # tokens
    keep=3,                # most recent tool results
    clear_tool_inputs=True,
    placeholder="[earlier tool result cleared to save context; "
                "search again if you need it]",
)])
```

Three details worth noticing, because they are each a decision:

**It clears tool results, not messages.** The questions and answers stay — they
are small and carry the thread of the conversation. The 3 KB retrieval excerpts
go. That is the right thing to drop: high-volume, low-value-after-use.

**It keeps the 3 most recent.** The current turn always still has its evidence.
Clear everything and the model loses the excerpts it is mid-way through citing.

**The placeholder is an instruction.** *"search again if you need it"* — so the
model can recover the dropped information rather than being silently blinded.
A placeholder that just said `[removed]` would be strictly worse.

And the threshold, from `config.py`:

> The library default is 100,000, which is meaningless against an 8,000
> tokens-per-minute budget.

**A library default tuned for a paid tier is actively wrong on a free one.**
100,000 would never fire before Groq rejected the request.

## Level 3 — persist it: the checkpointer

```python
graph = create_agent(model, tools=tools, checkpointer=checkpointer)
...
config = {"configurable": {"thread_id": session_id}}
result = await agent.graph.ainvoke({"messages": [HumanMessage(...)]}, config)
```

That is the entire persistence API. You pass a `thread_id`; LangGraph loads
that thread's state before the run and saves it after. **You never write a
`SELECT` or an `INSERT`.**

Note the input is only the *new* message. LangGraph merges it into the loaded
state. This confuses people at first: you are not sending history, you are
sending a delta to a keyed state.

### What's in the file

`data/conversations.sqlite3` has two tables:

| Table | Holds |
|---|---|
| `checkpoints` | one row per graph step: `thread_id`, `checkpoint_id`, `ts`, and the serialised state |
| `writes` | the individual channel updates that produced each checkpoint |

**A checkpoint is saved after every graph step, not every turn.** So one
question that calls two tools writes ~5 checkpoints. `run.py` counts the real
ones on disk. This matters for two reasons:

- the file grows faster than you would guess
- you get **time-travel for free** — every intermediate state is addressable,
  which is what makes LangGraph's interrupt/resume work

## The consequence nobody mentions: listing conversations is awkward

A UI needs "recent conversations, newest first, with titles". The checkpointer
API is not built for that — it stores state per thread, not a conversation
index. So `app/agent.py` does this:

```python
async for tup in lister(None, limit=limit * 12):
    ...
    # alist yields newest first, so the first sighting of a thread is
    # its latest state.
    if entry is None or candidate["messages"] > entry["messages"]:
        threads[thread_id] = candidate
```

Read that `limit * 12`. To show 40 conversations it scans **480 checkpoints**,
because there are several per turn, then groups by `thread_id` in Python and
keeps the largest per thread.

This is the clearest example in the codebase of **using a tool slightly outside
what it was designed for.** The checkpointer is a state store, not a
conversation index. The right fix is a small `conversations` table of your own
— `(session_id, title, updated_at, turn_count)` — updated on each turn. Then
listing is one indexed query instead of a 480-row scan with `× 12` chosen by
feel.

The comment about the title is a good product instinct, though:

> The first user message doubles as the title -- which is what someone scanning
> a list actually recognises.

## Deleting: the defensive dance

```python
for name in ("adelete_thread", "delete_thread"):
    deleter = getattr(saver, name, None)
    ...
logger.info("Checkpointer cannot delete threads; %s left in place", ...)
```

Not all checkpointer implementations support deletion, and the async/sync
naming differs. So it tries both, and if neither exists it **logs and continues
rather than raising**. Reasonable: failing to forget a conversation should not
break the request.

It is also a symptom — `BaseCheckpointSaver` is a young interface with uneven
implementations.

## Graceful degradation in `api.py`

```python
try:
    saver = await _resources.enter_async_context(
        AsyncSqliteSaver.from_conn_string(str(settings.conversation_db)))
except Exception as exc:
    logger.warning("Could not open the conversation database (%s); "
                   "falling back to in-memory history for this run", exc)
    return InMemorySaver()
```

Losing history is much better than refusing to start. Note also the
`AsyncExitStack`: `from_conn_string` is an async context manager, but the
connection must outlive the function that creates it, so it is entered on a
process-wide stack instead of with `async with`. A small, genuinely tricky
piece of lifetime management.

## The kind of memory this is *not*

Worth being precise, because "memory" is overloaded:

| Kind | What it means | Here? |
|---|---|---|
| **conversation history** | re-send the messages | **yes** — this lesson |
| **summarised history** | LLM compresses old turns into a paragraph | no |
| **semantic / long-term memory** | facts about the user in a vector store, retrieved per turn | no |
| **user profile** | structured preferences in a normal table | no |
| **cross-session memory** | "last time you preferred budget hotels" | no |

NorthStar has conversation history and nothing else. A conversation forgets
everything the moment you start a new one. That is a reasonable scope choice —
and worth knowing, because "add memory" usually means one of the other four.

## Alternatives

### Where to store conversation state
| Store | Survives restart | Concurrent | Notes |
|---|---|---|---|
| a python list | no | no | fine for a script |
| `InMemorySaver` | no | per-process | **used by the smoke scripts** |
| **`AsyncSqliteSaver`** | yes | single-writer | **used here.** One file, zero ops |
| `PostgresSaver` | yes | yes | the production answer for multi-instance |
| Redis checkpointer | yes | yes | fast, TTL-able, less durable |
| your own table | yes | yes | most control; you write the queries |

**The limit worth knowing:** SQLite is single-writer. Two app instances on one
file will hit `database is locked`. `AsyncSqliteSaver` is the right choice for
one process and the wrong choice the moment you scale horizontally — that is
the migration trigger, not file size.

### How to keep context under control
| Technique | Cost | Trade-off |
|---|---|---|
| **clear old tool results** | free | **used here.** Loses evidence; placeholder says to re-search |
| sliding window (last N turns) | free | forgets the start of the conversation |
| summarise old turns with an LLM | one extra call | keeps the gist, loses detail, adds latency |
| trim by token count | free | needs a tokeniser to be exact |
| vector-retrieve relevant past turns | an embedding per turn | scales to very long histories; complex |
| provider prompt caching | cheaper, not smaller | Anthropic/OpenAI cache the prefix; **does not help a hard context limit** |

For this app, clearing tool results is well-targeted: the excerpts *are* the
bulk, and they are re-fetchable. Summarisation would be the next step if
conversations got long.

## Run it

```bash
.venv/Scripts/python.exe learn/11-memory/run.py
```

It builds memory from scratch as a list, measures the growth curve with real
token counts, implements tool-result clearing by hand, then opens the actual
`data/conversations.sqlite3` — showing its schema, how many checkpoints exist
per turn, what a serialised checkpoint contains, and reproducing the
`limit * 12` scan that `list_conversations` has to do.

## Next

[12 — Serving](../12-serving/) — the HTTP layer.
