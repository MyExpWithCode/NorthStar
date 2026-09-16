# 08 — Tool calling: the model never calls anything

**Real files:** [app/rag/kb_tool.py](../../app/rag/kb_tool.py),
[app/agent.py](../../app/agent.py) (`extract_provenance`)
**Libraries:** `langchain-core` (`@tool`), the provider's `tools` parameter

**This lesson needs `GROQ_API_KEY`.** It is the most important lesson here.

> **Rate limits.** This lesson makes several model calls. Groq's free tier
> meters tokens per minute, so on a fresh budget it runs in ~20 s, and on an
> exhausted one you will see lines like
> `[HTTP 429; waiting 51s, retry 1/4]` while it backs off and retries rather
> than dying. That is the same failure `max_retries=5` in
> [app/llm.py](../../app/llm.py) exists for.


## The one sentence

> **The model has no network, no filesystem and no ability to execute
> anything. It replies with JSON saying which function it would like called.
> Your code calls it and sends the result back as another message.**

Everything labelled "AI agent" is built on that. Nothing else is going on.

## The four-step dance

```
  1. YOU  -> model     here is the question, and here are 3 functions
                       you may ask me to run (as JSON schemas)

  2. model -> YOU      I want get_weather_forecast({"city":"Singapore",
                       "days":3}). I have no answer for you yet.
                       finish_reason: "tool_calls"

  3. YOU  -> YOU       you run the function. The model waits. It has no
                       idea whether you will comply, or lie.

  4. YOU  -> model     role:"tool", content:'{"forecast":[...]}'
                       ...and the model writes prose, or asks for
                       another tool (back to step 2)
```

Step 3 is the whole point. **The model is asking permission, not taking
action.** You can refuse, sanitise, log, rate-limit, or fake the result — and
the model cannot tell.

That is also why lesson 06's provenance guarantee holds: your code sees every
tool result before the model does.

## What you actually send

A tool is a **JSON Schema** describing a function signature:

```json
{
  "type": "function",
  "function": {
    "name": "get_weather_forecast",
    "description": "Get the weather forecast for a city, up to 16 days ahead.",
    "parameters": {
      "type": "object",
      "properties": {
        "city":  {"type": "string", "description": "City name, e.g. Singapore"},
        "days":  {"type": "integer", "description": "How many days, 1-16"}
      },
      "required": ["city"]
    }
  }
}
```

**The `description` fields are prompt engineering.** They are the only thing
the model knows about your function. This is why `kb_tool.py`'s docstring reads
like an instruction manual rather than API docs:

> The only permitted source of destination facts [...] Do not answer
> destination questions from your own knowledge. Not for weather or exchange
> rates.

That text goes straight into the request. A vague description is a bug, and it
manifests as the model picking the wrong tool — which looks like a model
problem and is actually a writing problem.

## What comes back

```json
{
  "choices": [{
    "finish_reason": "tool_calls",
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {
          "name": "get_weather_forecast",
          "arguments": "{\"city\":\"Singapore\",\"days\":3}"
        }
      }]
    }
  }]
}
```

Three things people trip on:

1. **`content` is `null`.** There is no answer yet. Print it and you get
   nothing.
2. **`arguments` is a *string*, not an object.** It is JSON-inside-JSON and you
   must `json.loads` it. It can also be malformed, because a model generated it.
3. **`id` matters.** Your tool result must carry the matching
   `tool_call_id`, or the provider rejects the next request. With parallel tool
   calls there are several, and each needs its own reply.

## `@tool` — what LangChain automates

Hand-writing that schema for every function is miserable. So:

```python
@tool("search_travel_knowledge_base", response_format="content_and_artifact")
def search_travel_knowledge_base(
    query: str,
    destination: Annotated[str, "The place the question is about..."] = "",
    categories: Annotated[list[str], "Optional single-tag filter..."] = [],
    k: Annotated[int, "How many excerpts. 0 means the default of 5."] = 0,
) -> tuple[str, dict]:
    """Search the travel knowledge base for destination facts. ..."""
```

The decorator reads your **type hints**, **`Annotated` descriptions**,
**defaults** and **docstring**, and generates the JSON Schema. `run.py` prints
the generated schema next to a hand-written one so you can compare.

That is the single highest-value thing `langchain-core` does in this project.

### `content_and_artifact`: the provenance mechanism

This is the design detail most worth stealing. The tool returns **two** things:

```python
return content, artifact
```

| Return value | Goes to | Why |
|---|---|---|
| `content` | the model, as the tool message | numbered excerpts it can cite as `[S1]` |
| `artifact` | **your code only** | the source list: URL, title, licence, score |

The artifact never enters the prompt. So when `agent.py` builds the citation
panel it reads the artifact, not the model's text:

```python
artifact = message.artifact          # {"sources": [...]}
```

**Consequence: if `[S1]` appears in the answer, a real retrieved chunk produced
it.** The model cannot invent a source that does not exist. It can still
mis-attribute — cite `[S1]` for a claim `[S2]` supports — but the URL and
licence shown in the UI came from `index.pkl`, not from a model.

It also saves tokens: the source URL is deliberately *not* sent to the model,
because it would cost tokens on every call of the loop and the UI already has
it.

## A real production scar worth reading

From `kb_tool.py`:

```python
#: Deliberately NOT expressed as a Literal enum in the tool schema. Groq
#: validates tool arguments server-side and rejects the whole call with a 400
#: when a model invents a value -- observed with `"family"`, which killed the
#: chat turn outright.
VALID_CATEGORIES = ("attractions", "neighbourhoods", ...)
```

The "correct" schema is an enum. Groq enforces enums server-side, so when the
model invented `"family"` the **entire request 400'd** and the user's turn died.

The fix is instructive: declare `list[str]`, validate inside the tool, drop
unknown values, and *tell the model which ones you ignored*:

```
(Ignored unknown category filter(s): family. Valid values are: ...)
```

**Strict validation turned a recoverable mistake into a fatal error.** Lenient
validation plus an informative message let the model self-correct on the next
loop. This is a genuinely counter-intuitive lesson about designing interfaces
for a non-deterministic caller.

## Alternatives

### How to get structured behaviour out of a model
| Approach | Reliability | Notes |
|---|---|---|
| **native tool calling** | high | **used here.** Provider-trained. Requires provider support |
| JSON mode / `response_format` | high | structured output, but no function dispatch |
| constrained decoding (Outlines, `llama.cpp` grammars) | **guaranteed** | enforces the grammar at sampling time. Local models |
| Instructor / Pydantic AI | high | tool calling + pydantic validation + retries |
| ReAct prompting (`Thought:/Action:`) | low | the pre-tool-calling era. Parse text, pray. Historical interest |
| few-shot + regex parsing | very low | don't |

**ReAct is worth knowing about historically.** Before native tool calling
(2023), you prompted `Thought: ... Action: search[query]` and parsed the text.
Brittle. If you read older LangChain tutorials mentioning `AgentExecutor` and
`ZeroShotAgent`, that is what they are doing, and it is obsolete.

### How to define tools
| Approach | Notes |
|---|---|
| hand-written JSON Schema | full control, tedious, drifts from the code |
| **LangChain `@tool`** | **used here.** Schema from type hints + docstring |
| pydantic model as the schema | more precise validation |
| `mcp` `@mcp.tool()` | same idea, but exposed over a protocol. Lesson 09 |
| OpenAI SDK `pydantic_function_tool` | provider-native equivalent |

## The failure mode to design for

The model will, eventually:

- call a tool that does not exist
- omit a required argument
- pass `"3 days"` where you wanted `3`
- invent an enum value (see above)
- call the same tool five times in one turn
- ignore your tools and answer from memory anyway

None of these are bugs you can prevent. They are inputs you must handle.
`run.py` demonstrates several deliberately. Notice which ones `kb_tool.py`
handles gracefully and which ones would still kill a turn.

## Run it

```bash
.venv/Scripts/python.exe learn/08-tool-calling/run.py
```

It builds tool schemas by hand, gets a real tool call back and prints the raw
JSON, completes the four-step dance manually, then shows `@tool` generating the
same schema automatically. It demonstrates that **you** can lie to the model
about a tool result, shows the enum-400 scar, and tests what happens when the
model asks for two tools at once.

## Next

[09 — MCP](../09-mcp-protocol/) — the same idea, over a protocol.
