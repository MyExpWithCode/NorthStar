# 06 — Retrieval: how you stop the model inventing facts

**Real files:** [app/rag/retriever.py](../../app/rag/retriever.py),
[app/rag/kb_tool.py](../../app/rag/kb_tool.py)
**Library:** none new — this lesson is design, not dependencies

Lessons 02–05 built a search engine. This lesson is about the thing that makes
it an *assistant you can trust*, which is a completely different problem.

## The failure this whole app is shaped around

Ask a language model "what time does the Bugis MRT station close?" and it will
tell you. Confidently. With a plausible time. It may well be wrong, and you
have no way to tell from the answer.

This is the failure mode that matters in a travel assistant, because a wrong
opening time is not an abstract inaccuracy — someone stands outside a closed
building. So NorthStar's design goal is not "answer well". It is:

> **"I don't know" must be a reachable outcome.**

Almost everything odd-looking in `retriever.py` and `kb_tool.py` serves that.

## Four guards, in order

### Guard 1 — the model has no other source of destination facts

The system prompt says so, and the KB tool's docstring repeats it:

> The only permitted source of destination facts [...] Do not answer
> destination questions from your own knowledge.

This is the weakest guard — it is instruction, not enforcement, and a model can
ignore it. It is still worth having, and note *where* it lives: in the tool's
docstring, which becomes the tool description the model sees. The instruction
travels with the tool. Lesson 08.

### Guard 2 — the relevance floor

```python
RELEVANCE_FLOOR = 0.60
if score < settings.relevance_floor:
    continue          # drop the chunk
```

If nothing clears 0.60, the tool returns the sentinel string
`NO_RELEVANT_CONTENT` plus an instruction to say so.

**The number is measured, not guessed.** From `config.py`:

> On this corpus, genuine travel questions score 0.62–0.86 and unrelated
> questions 0.49–0.63.

Read those two ranges again. **They overlap between 0.62 and 0.63.** The
comment says so explicitly and calls the floor "deliberately a COARSE guard".

That honesty is the most instructive thing in the file. A threshold cannot
separate two overlapping distributions. Whatever you pick:

- **higher** (0.70) → real questions get refused. False "I don't know".
- **lower** (0.50) → junk gets through and is presented as evidence.

`run.py` measures the actual distributions on this corpus and draws them, so
you can see the overlap rather than take it on faith.

### Guard 3 — send the score to the model and let it judge

Because the floor cannot be sufficient, `kb_tool.py` puts the score in the
excerpt header:

```
[S1] Wikivoyage: Singapore/Bugis > Eat > Budget (relevance 0.68)
Hawker food here is cheap and...
```

This is a genuinely good design move, and its reasoning is in the docstring:
*a threshold cannot decide whether retrieved text actually answers the
question, so the model is given the evidence to judge that itself.* A 0.61
match is shown, marked as weak, and the model can say "the guide mentions this
only in passing".

The floor stops *garbage*. The model handles *marginal*. Two different jobs.

### Guard 4 — destination coverage, checked separately

This is the subtlest one, and the comments defend it at length:

```python
NO_RELEVANT_CONTENT     = "NO_RELEVANT_CONTENT"
DESTINATION_NOT_COVERED = "DESTINATION_NOT_COVERED"
```

Two sentinels, deliberately not merged:

| Sentinel | Means | Honest answer |
|---|---|---|
| `DESTINATION_NOT_COVERED` | no documents about Rome at all | "I have no guide for Rome. I cover Singapore, Kyoto and Andhra Pradesh." |
| `NO_RELEVANT_CONTENT` | have Singapore docs, nothing on this topic | "My Singapore guide doesn't mention that." |

Collapsing them into one "not found" would let a Rome question be answered from
Singapore's guide — and lesson 04 showed why the vectors won't stop you:
"best neighbourhood for street food" embeds almost identically for any city.
Only metadata can enforce this. The floor cannot.

## Provenance: citations that cannot be faked

`agent.py` builds the citation list from the tool's **artifact**, not by
parsing the answer text:

```python
artifact = message.artifact          # {"sources": [...]}
```

The consequence is worth stating precisely: **if `[S1]` appears in the answer,
a real retrieved chunk produced it.** The model can still mis-attribute — cite
`[S1]` for a claim `[S2]` supports, or write a sentence the excerpt does not
support — but it cannot invent a source that does not exist. The URL, title and
licence shown in the UI came from `index.pkl`, not from the model.

That is a smaller guarantee than "the answer is correct", and it is honest
about being smaller. It is also the strongest guarantee available without a
separate verification pass.

## Two caps that look like premature optimisation and are not

```python
MAX_RETRIEVAL_K     = 5      # retriever.py  - hard ceiling on excerpts
EXCERPT_CHAR_LIMIT  = 520    # kb_tool.py    - chars sent per excerpt
```

Both have observed incidents behind them, recorded in the comments: the model
asked for 15 excerpts across three searches in one turn and Groq returned
HTTP 429/413. These are not guesses about scale — they are scars. Lesson 11.

## The bug lesson 05 found, and what it means here

Lesson 05 measured eight realistic conversational follow-ups. Result:

```
  destination      query                   above floor   delivered
  Kyoto            "where should I eat"            20          3
  Kyoto            "what should I see"              2          2
  Andhra Pradesh   "where should I eat"             0          0
```

Two distinct problems, both real:

**1. Short queries score low against everything.** Lesson 04 explained why —
short query vs long document is a systematic geometry mismatch in bge models.
`"what should I see"` clears 0.60 against only 2 of 349 Kyoto chunks. The floor
was calibrated on full questions like *"how do I get from the airport to the
city centre"*, and a conversational fragment behaves nothing like that.

The standard fix is **query rewriting**: have the model expand
`"where should I eat"` into `"where to eat in Kyoto, restaurants and local
food"` before searching. The architecture already permits it — the model
chooses the query string — but nothing in the prompt asks for it.

**Measure that fix carefully, though.** My first attempt scored it by counting
chunks above the floor, and got an absurd "+329 chunks" improvement. The reason
is a trap worth internalising:

```
  query                              best    mean   >= 0.60
  "what should I see"               0.635   0.499     2/349
  "...temples to see in Kyoto"      0.792   0.679   331/349
  "Kyoto"                           0.807   0.675   344/349   <-- !
```

The bare word `"Kyoto"` — carrying no information need whatsoever — lifts 344
of 349 Kyoto chunks above the floor. Because lesson 03 **prepended the section
path before embedding**, and every Kyoto path starts `Wikivoyage: Kyoto/…`.
Naming the destination raises every chunk in that destination uniformly.

Score the rewrite by **ranking** instead and the improvement is real but
different: the raw fragment ranks `Kyoto/Arashiyama > Get around` first — a
*transport* section, for a question about sights — while the rewrite correctly
ranks `Kyoto/Central > See` first.

**And there is an uncomfortable consequence for the floor itself.** If naming
the place pushes ~95% of that place's chunks over 0.60, then for any query that
mentions the destination the floor barely discriminates at all. Its protective
value depends on how the model happens to phrase the query.

That fragility is invisible from reading `retriever.py`. It falls out of a
decision in `chunk.py` (prepend the section path) interacting with a decision
in `config.py` (floor = 0.60). Neither file mentions the other. **This is what
makes RAG pipelines hard to reason about: the behaviour lives in the
interactions, not in any single module.**

**2. Andhra Pradesh clears the floor for nothing.** Best score ~0.53 on a
fragment. Its source document is the junk one from lesson 03: Wikipedia
navigation menus, 14 of 16 chunks untagged, 23 chunks total. Rewriting does
lift its scores (the name is in the path) but the *content* is still nav
furniture. **Bad ingestion produces content that is technically retrievable and
practically useless, and no query-time tuning rescues it.** The bug is four
lessons upstream.

That is the most valuable debugging lesson in the whole folder: *"retrieval
returned nothing" has at least four distinct causes — ingestion quality,
chunking, query phrasing, and the threshold — and they are indistinguishable
from the outside.* Measure before you tune.

## Alternatives

| Technique | What it fixes | Cost |
|---|---|---|
| **fixed threshold** | garbage results | free. **used here.** Cannot handle overlap |
| **top-k only, no threshold** | nothing | free. Always returns something — dangerous here |
| **query rewriting / expansion** | short and vague queries | one LLM call per search. **Biggest available win** |
| **HyDE** | vocabulary mismatch | generate a fake answer, embed *that*, search with it |
| **hybrid search (BM25 + vector)** | exact names, rare terms | ~2× search cost. Standard in production |
| **cross-encoder reranking** | ranking precision | over-fetch 50, rerank with a slower pairwise model. Usually the single biggest quality gain |
| **LLM-as-judge relevance filter** | marginal results | an LLM call per chunk. Accurate, expensive |
| **RAG-Fusion / multi-query** | one phrasing missing results | generate 4 query variants, merge with RRF |
| **contextual retrieval** | chunks lacking context | prepend an LLM-written summary to each chunk at ingest |
| **self-RAG / CRAG** | knowing when to retrieve | the model critiques its own retrieval and retries |

Ranked by value for *this* codebase, on the evidence in these lessons:

1. **query rewriting** — directly fixes the measured failure above
2. **reranking** — cheap to add, reliably improves ranking quality
3. **fix the `url-contents` ingestion path** — lesson 03's defect
4. **raise `fetch_k`** from `k*6` to `k*12` — lesson 05's measurement
5. hybrid search — robustness, no observable failure yet

## Run it

```bash
.venv/Scripts/python.exe learn/06-retrieval/run.py
```

It plots the real score distributions for good and bad queries against this
corpus (so you can see the overlap the comment describes), shows what moving
the floor to 0.50 and 0.70 would actually admit and reject, demonstrates both
sentinels, and tests whether query rewriting fixes the measured failure.

## Next

[07 — Calling an LLM](../07-call-an-llm/) — the first lesson that needs a key.
