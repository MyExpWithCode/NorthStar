# 07 — Calling an LLM: what is underneath `ChatGroq(...)`

**Real file:** [app/llm.py](../../app/llm.py)
**Libraries:** `langchain-groq` → `groq` SDK → HTTP

**This lesson needs `GROQ_API_KEY` in `.env`.**

> **Rate limits.** This lesson makes several model calls. Groq's free tier
> meters tokens per minute, so on a fresh budget it runs in ~20 s, and on an
> exhausted one you will see lines like
> `[HTTP 429; waiting 51s, retry 1/4]` while it backs off and retries rather
> than dying. That is the same failure `max_retries=5` in
> [app/llm.py](../../app/llm.py) exists for.


## The whole thing is one HTTP POST

```
POST https://api.groq.com/openai/v1/chat/completions
Authorization: Bearer gsk_...
Content-Type: application/json

{
  "model": "openai/gpt-oss-120b",
  "messages": [
    {"role": "system", "content": "You are a travel assistant."},
    {"role": "user",   "content": "What is the capital of Singapore?"}
  ],
  "temperature": 0
}
```

Response:

```json
{
  "id": "chatcmpl-...",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "Singapore is a city-state..."},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 28, "completion_tokens": 41, "total_tokens": 69}
}
```

That is it. No websockets, no streaming required, no state on the server. **A
language model API is a stateless function from a list of messages to one more
message.** `run.py` does exactly this with `urllib` — no SDK, ~10 lines.

Internalising "stateless" now saves confusion in lesson 11: the model
remembers nothing. If a conversation appears to have memory, it is because
**you** re-sent the entire history.

### One thing the real response has that the docs example doesn't

`run.py` prints the response unedited, and with `gpt-oss-120b` you will see two
extra fields:

```json
"message": {
  "role": "assistant",
  "content": "Singapore uses the Singapore dollar (SGD).",
  "reasoning": "The user asks: ... So respond with a short sentence: ..."
},
"usage": {
  "prompt_tokens": 92, "completion_tokens": 69,
  "completion_tokens_details": {"reasoning_tokens": 51}
}
```

A 42-character answer cost 69 completion tokens, **51 of them reasoning**. You
are billed for tokens you never see and cannot use. That is normal for
reasoning models and worth knowing when you are budgeting against a
tokens-per-minute limit — it is roughly 3× what the visible answer suggests.

## The three roles

| Role | Who wrote it | Purpose |
|---|---|---|
| `system` | you | standing instructions, sent every time |
| `user` | the person | the question |
| `assistant` | the model | its previous replies (you send them back for context) |

Later, lesson 08 adds a fourth — `tool` — and that is the entire mechanism by
which an agent works.

## Why the API path says `/openai/v1/`

Because `app/llm.py`'s docstring had to explain this, and it is worth
repeating:

> the default resolves to `openai/gpt-oss-120b`, which is served **by Groq**,
> not by OpenAI.

Two separate things cause confusion here:

1. **`/openai/v1/` is Groq's OpenAI-*compatible* endpoint.** The OpenAI chat
   API became the de-facto standard, so almost every provider offers a
   drop-in-compatible path. Nothing is sent to OpenAI.
2. **`openai/gpt-oss-120b` is a model *name*.** GPT-OSS is OpenAI's
   open-*weight* model; Groq namespaces catalogue entries by who published the
   weights, the same way it lists `qwen/...` under Alibaba. Every request goes
   to `api.groq.com` with `GROQ_API_KEY`. There is no OpenAI credential in this
   project.

The practical upside of that standard: swapping Groq for Together, Fireworks,
DeepInfra, vLLM or Ollama is usually a base-URL change.

## The gotcha that cost me real time writing this

The raw `urllib` call returns **HTTP 403 Forbidden** unless you set a
`User-Agent` header. No auth error, no useful body — just 403, with a valid
key.

```python
headers = {
    "Authorization": f"Bearer {key}",
    "Content-Type": "application/json",
    "User-Agent": "northstar-learn/0.1",     # <- without this, 403
}
```

Python's default `User-Agent: Python-urllib/3.14` is rejected by the edge/WAF in
front of the API. Exactly the same failure as Wikimedia in lesson 02.

You never see this through the SDK, because `groq` (via `httpx`) sets a
sensible User-Agent for you. **That is a real, concrete thing a client library
buys you** — a class of infrastructure-level failure you never learn exists.
Worth remembering next time a dependency looks like pure overhead.

## Model resolution: the interesting part of `app/llm.py`

Most tutorials hardcode `llama-3.3-70b-versatile`. `app/llm.py` refuses to:

```python
PREFERRED_GROQ_MODELS = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b")
```

It fetches `GET /models` at startup and picks the first preference that is
actually available. The docstring explains why, and it checks out — the live
catalogue right now has **13 models and not one `llama-3.3-*` id**. Every
tutorial naming one is already broken.

Two filters worth understanding:

```python
_NOT_CHAT_MODELS = ("whisper", "orpheus", "prompt-guard", "tts", "embed", "guard")
_EXCLUDED_GROQ_PREFIXES = ("groq/compound",)
```

The first drops speech-to-text, text-to-speech and safety classifiers — they
are in the same catalogue but cannot chat. The second is more interesting:
Groq's `compound` models are **agentic systems with their own built-in tools**.
This app supplies its own tools and does its own tool selection, so mixing the
two would make it impossible to say which tool produced which fact. Excluding
them protects the provenance guarantee from lesson 06.

## Settings that matter

```python
options = {"temperature": 0, **overrides}
```

**`temperature=0`** — near-deterministic. The docstring: *"this assistant
reports retrieved facts and tool results, so run-to-run variation in what it
claims is a liability rather than a feature."* Exactly right for RAG. You would
want 0.7+ for creative writing.

**`max_retries=5`** for Groq, with a comment: the free tier meters tokens per
minute and retrieval-heavy turns hit HTTP 429. The SDK backs off and retries,
turning a transient limit into a slower answer rather than a failed one. Note
this is *only* set for Groq — Anthropic's client has its own defaults.

**`max_tokens=8192`** for Anthropic only, because Anthropic requires it and
Groq does not.

## The three layers, and what each adds

```
  urllib POST                   10 lines. You build the JSON by hand.
      |
  groq SDK                      typed responses, User-Agent, retries,
      |                         streaming, pagination
  langchain-groq (ChatGroq)     LangChain's Message objects, .bind_tools(),
                                .invoke()/.ainvoke(), callbacks, tracing
```

What the LangChain layer actually buys — and it is a genuine question worth
asking, since it is a large dependency:

- **provider-swappability**: `ChatGroq` → `ChatAnthropic` with no other change.
  `app/llm.py` uses exactly this, and it is why NorthStar supports two
  providers in ~40 lines.
- **`.bind_tools(tools)`**: converts python functions into JSON schemas.
  Lesson 08 shows how much hand-written JSON that saves.
- a common `Message` vocabulary that LangGraph, the checkpointer and LangSmith
  all speak.

The cost: two more layers between you and the HTTP call, and stack traces that
go through both.

## Alternatives

### Providers
| Provider | Strength | Watch out for |
|---|---|---|
| **Groq** | very fast, generous free tier | **used here.** Small model catalogue, aggressive rate limits |
| **Anthropic** (Claude) | strongest reasoning + tool use | no free tier |
| OpenAI | ecosystem, reliability | cost |
| Google (Gemini) | huge context, cheap | different API shape |
| Together / Fireworks / DeepInfra | many open models, cheap | quality varies |
| **Ollama** | **fully local, free, private** | needs a good GPU for large models |
| vLLM / TGI | self-hosted serving at scale | you operate it |
| AWS Bedrock / Azure OpenAI | enterprise compliance | more setup |

### Client layers
| Layer | When to use it |
|---|---|
| raw HTTP | you want zero dependencies and one provider |
| provider SDK (`groq`, `anthropic`) | **one provider, want it done properly.** Often the right answer |
| LangChain | **used here.** Multiple providers, or you want its agent/tool ecosystem |
| LiteLLM | you want 100+ providers behind one OpenAI-shaped interface, nothing else |
| Instructor / Outlines | you mainly want guaranteed structured output |
| Pydantic AI | typed agents, lighter than LangChain, newer |
| DSPy | you want to *optimise* prompts programmatically rather than write them |

**Honest assessment for this app:** if it only ever used Groq, the `groq` SDK
plus a hand-written tool loop (lesson 10, ~40 lines) would be simpler than
LangChain. LangChain earns its place here through `create_agent` + LangGraph
checkpointing + LangSmith tracing + MCP adapters as a bundle — not through the
chat call itself.

## Run it

```bash
.venv/Scripts/python.exe learn/07-call-an-llm/run.py
```

It makes the same request four ways — raw `urllib`, the `groq` SDK, LangChain
`ChatGroq`, and a deliberate 403 to show the User-Agent trap — prints the full
unedited JSON, lists the live model catalogue with the filters applied, and
demonstrates that the API is stateless by asking a follow-up question without
history.

## Next

[08 — Tool calling](../08-tool-calling/) — the most important lesson here.
