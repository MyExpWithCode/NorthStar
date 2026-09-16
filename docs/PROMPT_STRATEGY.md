# Prompt and context strategy

Implemented in [`app/prompts.py`](../app/prompts.py). This document explains *why* each rule is there,
which is the part the brief (§5) asks for.

## The problem the prompt has to solve

The assistant draws on three sources that are easy to confuse in a fluent answer:

| Source | Trust | Failure if confused |
|---|---|---|
| Knowledge base (RAG) | high, but only for what it contains | stale or invented destination facts |
| MCP tools | high, but only at the moment retrieved | a plausible made-up forecast or rate |
| The model itself | useful for judgement, not for facts | fabrication presented as retrieval |

A model left to its own devices will happily merge all three into one confident voice. Every rule below
exists to keep them separable.

## Rule order is deliberate

The prompt is ordered by what breaks worst when ignored:

1. **Sourcing** — where facts may come from.
2. **Tool selection** — which tool for which question.
3. **Honesty about gaps** — what to do when the answer isn't available.
4. **Structure and labelling** — how the answer is presented.
5. **Preferences** — what to carry across turns.

Models attend unevenly to long instructions, so the non-negotiable constraint (never invent a destination
fact) is stated first and in absolute terms.

## The rules, and the reasoning

### 1. Exactly three sources, each with a named owner

The prompt names `search_travel_knowledge_base` as the *only* permitted source of destination facts, and
the MCP tools as the *only* permitted source of live information. Phrasing it as an exclusive channel
rather than a preference ("use the knowledge base") removes the model's room to supplement from memory —
which is the most likely way a wrong fact enters an answer, because the model's own Singapore knowledge is
extensive and plausible.

The third source — the model's own reasoning — is **explicitly permitted**, because sequencing a day and
recommending an order genuinely is the model's job. Forbidding it outright would produce either a refusal
or a covert violation. Permitting it *with a label* is what makes the distinction enforceable.

### 2. Negative tool-selection instructions, not just positive ones

The brief states MCP must not be used for questions the knowledge base already covers. Saying "use the KB
for destination questions" is not enough; the prompt also says **"do NOT call a weather or currency tool
for something the knowledge base covers"**. Prohibitions catch the failure mode that permissions miss.

The prompt also explicitly authorises **repeat calls**: "One search rarely covers a multi-day itinerary."
Without that, models tend to make a single retrieval and then extrapolate.

### 3. The indoor-swap instruction is procedural, not aspirational

For the flagship scenario, the prompt gives a concrete procedure: when a forecast day shows poor outdoor
conditions, **search the knowledge base again with `categories: ["indoor"]`**. It names the tool, the
argument and the trigger. "Suggest indoor alternatives when it rains" would invite the model to invent
indoor attractions from memory; naming the retrieval call keeps the alternatives grounded.

This is why the `indoor` / `outdoor` tags exist at ingest time — the prompt depends on them.

### 4. Relevance scores are shown to the model, with a warning

Retrieval calibration ([ARCHITECTURE.md §6.1](ARCHITECTURE.md)) showed that no similarity threshold
separates answerable from unanswerable questions: "best ski resorts in Singapore" scores 0.713, above a
legitimate question about family activities at 0.622. A threshold alone therefore cannot implement "state
when information is unavailable".

So the tool returns each excerpt **with its score**, and the prompt says: *"A high score does not mean the
passage answers the question. If the retrieved text does not actually contain the answer, say so rather
than stretching it into one."* The mechanical floor handles the obvious cases; this instruction handles the
cases the floor provably cannot.

### 5. Three distinct failure sentinels, three distinct responses

| Sentinel | Meaning | Required response |
|---|---|---|
| `NO_RELEVANT_CONTENT` | nothing above the floor | say the KB does not cover it; offer what it does |
| `KNOWLEDGE_BASE_UNAVAILABLE` | no index built | say the knowledge base is unavailable |
| `"ok": false` on a tool result | upstream service failed | name what could not be retrieved, and why |

Each is a machine-checkable token rather than a tone the model has to infer. The prompt states the
preference explicitly — *"It is better to say 'I could not reach the weather service' than to name a
temperature"* — because the default instinct of a helpful model is to produce *something*.

### 6. A fixed provenance vocabulary

Three labels, used verbatim:

- **📚 From the knowledge base** — with `[S1]`-style citation markers
- **🌐 Live via MCP — `<tool>` (retrieved `<timestamp>`)** — the real timestamp from the tool result
- **💡 Suggestion** — the model's own reasoning

They are constants in `app/prompts.py` (`LABEL_KB`, `LABEL_MCP`, `LABEL_SUGGESTION`) so the prompt, the UI
and this document cannot drift apart. Requiring the **timestamp inside the label** is what makes "live"
verifiable rather than a claim; for exchange rates the prompt also requires the rate's publication date,
because ECB rates are a business-day snapshot, not a live quote.

### 7. Preferences persist without being restated

The prompt lists the preference types to retain (budget, children, dietary, pace, mobility, dates,
interests) and requires the assistant to **say briefly when it applied one**. Naming the categories works
better than "remember user preferences", and the acknowledgement makes retention observable — which is how
the multi-turn acceptance criterion gets demonstrated rather than asserted.

## Context strategy

**Conversation memory.** A LangGraph checkpointer keyed by `thread_id` (the session id) holds the full
message history, so tool calls and their results stay in context and the model can refer back to a
forecast it already fetched instead of re-fetching it.

**The system prompt is assembled per request, not a constant**, because two things vary:

- **Today's date** is injected. A model that guesses the date silently mis-plans "next week" — and the
  weather tool needs concrete ISO dates.
- **Tool availability** is injected from `McpToolset.prompt_note()`. When a server is down, the prompt
  names the missing capability so the assistant can say the forecast is unavailable. A tool absent from
  the tool list cannot be hallucinated into a result, and a capability named as unavailable cannot be
  quietly worked around.

**Destination and currencies** come from configuration rather than being hardcoded in the prose, so the
same prompt serves a different destination without editing.

## What is deliberately *not* in the prompt

- **No few-shot examples.** They would bias the answer format toward the examples' content and consume
  context on every turn. The label vocabulary plus structural instructions proved sufficient.
- **No "you are an expert travel agent" persona.** It buys nothing here and encourages confident prose,
  which is the opposite of what a grounded assistant needs.
- **No instruction to be concise at the expense of citations.** Provenance is the product.
