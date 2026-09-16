# 10 — The agent loop: it is a `while` loop

**Real files:** [app/agent.py](../../app/agent.py),
[app/prompts.py](../../app/prompts.py)
**Libraries:** `langchain` (`create_agent`), `langgraph`

**Needs `GROQ_API_KEY`.** This is where lessons 06–09 combine.

> **Rate limits.** This lesson makes several model calls. Groq's free tier
> meters tokens per minute, so on a fresh budget it runs in ~20 s, and on an
> exhausted one you will see lines like
> `[HTTP 429; waiting 51s, retry 1/4]` while it backs off and retries rather
> than dying. That is the same failure `max_retries=5` in
> [app/llm.py](../../app/llm.py) exists for.


## The whole thing

```python
while True:
    response = call_model(messages, tools)
    if not response.tool_calls:
        return response.content          # done, it wrote prose
    for call in response.tool_calls:
        result = REGISTRY[call.name](**call.args)
        messages.append(tool_message(call.id, result))
```

**That is an agent.** `run.py` implements it in about 40 lines, with the real
knowledge base and the real MCP servers, and it handles the flagship scenario.

Everything in `create_agent` is that loop plus production concerns. Worth
knowing so the framework stops feeling like magic — and so you can tell when
you need it.

## Three things the naive loop gets wrong

`run.py`'s version is honest about these. They are the reasons to use a
framework:

**1. No iteration cap.** A model that keeps calling tools loops forever, and
each iteration costs money. Always bound it.

**2. Sequential tool execution.** Two independent tool calls in one turn should
run concurrently. The naive loop does them one after another.

**3. Context grows without limit.** Every tool result stays in `messages`
forever. Turn 3 of a conversation exceeds the provider's limit. This one bit
NorthStar for real — see below.

## What `create_agent` actually is

```python
graph = create_agent(
    model,
    tools=tools,
    system_prompt=prompts.system_prompt(...),
    checkpointer=checkpointer,
    middleware=[context_editing],
)
```

It builds a **LangGraph state machine** with two nodes:

```
         ┌──────────┐
    ─────►  model   ├──── no tool calls ────► END
         └────┬─────┘
              │ tool_calls
         ┌────▼─────┐
         │  tools   ├──────────┐
         └──────────┘          │
              ▲                │
              └────────────────┘
```

Same loop, drawn as a graph. Why bother with the graph formalism? Because once
the loop is data rather than control flow, you can:

- **snapshot the state** after every step → that is the checkpointer, and
  therefore conversation memory (lesson 11)
- **insert middleware** at defined points → that is `ContextEditingMiddleware`
- **stream** intermediate steps to a UI
- **interrupt and resume** — human-in-the-loop approval before a tool runs
- add nodes for branching, routing, sub-agents

You cannot retrofit resumable, inspectable state onto a `while` loop without
essentially rebuilding LangGraph. **That is the real argument for it.**

## The design decision worth studying

From `agent.py`'s docstring:

> Tool selection is the model's job, driven by the tool descriptions and the
> system prompt. There is deliberately **no hand-written intent classifier**.

The temptation is obvious — write a router:

```python
if "weather" in question or "rain" in question:
    return weather_tool(...)
elif "convert" in question or "rupees" in question:
    return currency_tool(...)
else:
    return kb_search(...)
```

It fails, and the reason is specific and worth internalising. Take the flagship
question:

> *"Will it rain in Singapore on Thursday, and what should I do if it does?"*

The correct behaviour is:

1. search the KB for the planned itinerary
2. call `get_weather_forecast`
3. **look at the result.** If Thursday is `outdoor_suitability: "poor"` —
4. search the KB **again** with `categories=["indoor"]`
5. combine everything into one answer

**Step 4 depends on the *output* of step 2.** A keyword router cannot express
that, because the decision is not in the question — it is in the data that came
back. You would have to hand-code the dependency, and then again for every
other conditional path.

The loop handles it for free, because the model sees the tool result before
deciding what to do next. **That is the actual reason agents exist.** Not
"AI deciding things" — *conditional multi-step work where later steps depend on
earlier results.*

## The prompt is part of the mechanism

`app/prompts.py` is not decoration. Two things in it are load-bearing:

**The tool-choice rules make the flagship scenario reliable:**

> When a forecast day is poor for outdoor activity, search the knowledge base
> again with categories `["indoor"]` for real indoor alternatives. Never invent
> them.

That single sentence is the router you did not hand-write. And note it names
the `categories` filter specifically — which only works because lesson 03
tagged the chunks and lesson 04 showed why a text search for "indoor" would
fail.

**The brevity is deliberate, and the docstring explains the arithmetic:**

> The system prompt is re-sent on every model call in the agent loop, so each
> 1,000 characters here costs ~250 tokens per call and a multi-tool turn makes
> several. Groq's free tier allows 8,000 tokens per minute, which a verbose
> prompt alone can consume.

A four-iteration turn re-sends the system prompt four times. Prompt length is
a per-loop-iteration cost, not a one-off.

## Context editing: a real incident, not a precaution

```python
ContextEditingMiddleware(edits=[ClearToolUsesEdit(
    trigger=settings.context_trim_trigger_tokens,     # 4000
    keep=settings.context_trim_keep_results,          # 3
    clear_tool_inputs=True,
    placeholder="[earlier tool result cleared to save context; "
                "search again if you need it]",
)])
```

The comment records what happened:

> Retrieval excerpts accumulate in history and are the bulk of every request.
> Left alone, the third turn of a conversation gets rejected as too large.

And on the threshold:

> The library default is 100,000, which is meaningless against an 8,000
> tokens-per-minute budget.

That last point is the transferable one. **A library default tuned for a paid
tier is actively wrong on a free one.** 100,000 tokens would never trigger
before Groq rejected the request with HTTP 413.

Note what it keeps: the **3 most recent** tool results, so the current turn
always still has its evidence. Only older ones become the placeholder — and the
placeholder *tells the model it can search again*. Lesson 11.

## Provenance: only this turn's tools

```python
def _messages_since_last_human(messages: list) -> list:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return messages[index:]
    return messages
```

`result["messages"]` is the *entire* conversation, not just this turn. Without
this, turn 5's citation panel would re-list every tool called in turns 1–4.
A small function fixing an easy bug to ship.

## Failures must never look like answers

```python
def _failure_message(exc: Exception) -> str:
    if "RateLimit" in name or "429" in text:
        return ("The language model is rate-limited right now... "
                "No travel information was retrieved.")
```

Every branch says explicitly that **no travel information was produced**. The
reasoning is sound: a chat bubble containing an error, rendered in the same
place as answers, can be read as an answer. Saying "nothing above is a travel
fact" costs one sentence and removes the ambiguity.

(Writing these lessons hit exactly the 429 this handles. It is not
hypothetical.)

## Alternatives

| Approach | Lines | Gets you |
|---|---|---|
| **hand-written `while` loop** | ~40 | **lesson 13.** Full control, no deps. No memory, no streaming, no resume |
| `while` loop + provider SDK | ~60 | + retries, typed responses |
| **LangGraph `create_agent`** | ~10 | **used here.** Checkpointing, middleware, streaming, interrupts |
| LangGraph hand-built graph | ~50 | the same, but you define the nodes and edges — needed for non-linear flows |
| Pydantic AI | ~15 | typed agents, lighter, less ecosystem |
| OpenAI Agents SDK | ~15 | good handoffs/guardrails; OpenAI-centric |
| CrewAI / AutoGen | ~30 | **multi-agent** orchestration. Overkill for one agent |
| Semantic Kernel | ~30 | .NET-first, enterprise |
| DSPy | varies | optimises the prompt rather than hand-writing it |
| `smolagents` | ~20 | very small; agent writes python code instead of JSON calls |

**Honest recommendation for this app.** One agent, one tool loop. The hand-
written loop plus the `groq` SDK would be simpler and fully adequate for chat.
LangGraph earns its keep through **checkpointing** — persistent, listable,
resumable conversations (lesson 11) — and `ContextEditingMiddleware`. Take
those two requirements away and the framework is not paying for itself here.

If you were building this again: start with the `while` loop. Add LangGraph the
day you need persistence or human-in-the-loop. You will understand what it is
doing, because you will have written the thing it replaces.

## Run it

```bash
.venv/Scripts/python.exe learn/10-agent-loop/run.py
```

It runs a 40-line hand-written agent against the real knowledge base and the
real MCP servers, printing every loop iteration — which tool, which arguments,
what came back, how the token count grows. Then it runs the **flagship
scenario** so you can watch the model search the KB again for `indoor` content
*because* the forecast came back poor. Then the same question through
`create_agent` for comparison.

It makes several model calls, so on Groq's free tier expect rate-limit
backoff — the script waits and retries rather than dying.

## Next

[11 — Memory](../11-memory/) — how it remembers.
