# What measuring the app turned up

These lessons are teaching material, but writing them meant running the real
code against the real data, and that surfaced eight things worth acting on.
Listed most-actionable first. Each links to the lesson that measured it and
names the file to change.

None of these are guesses — every one has a reproduction in a `run.py` you can
execute.

---

## 1. `/admin`-ingested URLs bypass the HTML cleaning

**Severity: high — bad content is unreachable content**
**Found by:** [lesson 03](03-chunking/) · **File:** [app/ingest/parsers.py](../app/ingest/parsers.py)

There are two ingestion paths with very different cleaning:

| Path | Selectors | Section dropping |
|---|---|---|
| `fetch_sources.py` (curated) | **32** | yes — "References", "See also", … |
| `parsers.py` (`/admin` uploads and URLs) | **7** | no |

A URL added through `/admin` takes the second path. Result, live in
`data/index/`:

```
  chunks containing Wikipedia navigation furniture : 5
  chunks with no categories at all                 : 16  (14 of them url-contents)

  sample chunk text:
    'Andhra Pradesh\n\nTourism in Andhra Pradesh - Wikipedia  \nMain menu
     \nMain menu  \nmove to sidebar\nhide  \nNavigation  \n* Main page...'
```

The knock-on effect is measured in [lesson 06](06-retrieval/): Andhra Pradesh
clears the 0.60 relevance floor for **nothing**. Best score ~0.53 on a normal
question. The assistant tells users it has no coverage of a destination it
holds 23 chunks about.

**Fix:** have `parsers.py` reuse `fetch_sources.py`'s `_REMOVE_SELECTORS` and
`ALWAYS_DROP_SECTIONS` for HTML input, then re-ingest `url-contents`. An
untagged chunk is also invisible to `categories=["indoor"]` filtering, so it
cannot participate in the flagship scenario.

---

## 2. Duplicate `[S1]` citation markers within one turn

**Severity: high — the citation is ambiguous**
**Found by:** [lesson 08](08-tool-calling/) · **File:** [app/rag/kb_tool.py:225](../app/rag/kb_tool.py#L225)

```python
markers = ["S" + str(i) for i in range(1, len(hits) + 1)]
```

Markers restart at `S1` on **every tool call**, and `extract_provenance` in
`app/agent.py` deduplicates by `chunk_id` without renumbering. Any turn with
two knowledge-base searches — which the flagship scenario always does, once for
the itinerary and once for `indoor` alternatives — produces two distinct sources
labelled `[S1]`, two labelled `[S2]`, and so on.

Reproduced by running `mini.py`'s flagship question: the provenance list came
back with two separate `S1`–`S5` blocks.

**Fix:** number markers continuously across a turn rather than per call.
`learn/13-mini-app/mini.py` does this with a counter the agent resets each turn
(see `make_kb_tool`'s docstring).

Note the provenance *guarantee* still holds — citations cannot be fabricated,
because they come from the artifact. What broke is uniqueness of the label.

---

## 3. Short conversational queries fall below the relevance floor

**Severity: high — affects normal multi-turn use**
**Found by:** [lessons 05](05-vector-store/) and [06](06-retrieval/) · **File:** [app/prompts.py](../app/prompts.py)

The floor was calibrated on full questions. In conversation, turn 3 is a
fragment — the user already said where they are going, so the query text
carries no destination signal:

```
  destination=Kyoto  query="what should I see"     2 of 349 chunks clear 0.60
  destination=Kyoto  query="indoor activities"     3 of 349
  destination=Andhra Pradesh, any fragment         0
```

Same information need, different phrasing: `"what should I see"` scores 0.635
while `"main sights, attractions and temples to see in Kyoto"` scores 0.792 and
ranks a `> See` section first instead of a `> Get around` section.

**Fix (cheapest first):** add a line to the system prompt telling the model to
search with full descriptive queries rather than fragments. `mini.py` includes
exactly this and it is one sentence:

> Prefer full descriptive queries over short fragments: "where to eat in Kyoto,
> restaurants and local food" retrieves far better than "where to eat".

A retrieval-time rewriting step is the more robust version.

**Measurement warning, from lesson 06:** do not score this by counting chunks
above the floor. Because `chunk.py` prepends the section path before embedding,
merely naming the destination lifts ~95% of that destination's chunks over
0.60 — the bare word `"Kyoto"` puts 344 of 349 above it. Score by **ranking**.

---

## 4. `fetch_k = k * 6` truncates results on an unbalanced index

**Severity: medium — silent, partial results**
**Found by:** [lesson 05](05-vector-store/) · **File:** [app/rag/retriever.py](../app/rag/retriever.py)

FAISS `IndexFlatIP` cannot filter, so the retriever over-fetches and filters in
Python. With Singapore at 71% of the index, a minority destination gets crowded
out of the window:

```
  destination=Kyoto, query="where should I eat"
      chunks above the floor, index-wide : 20
      delivered with fetch_k = k*6 (30)  :  3
      delivered with fetch_k = k*12 (60) :  5   <- all of them
```

**Fix now:** change the multiplier from 6 to 12 — one character.
**Fix properly:** a store that filters inside the index (Qdrant, Chroma,
pgvector) always returns `k` matching results and cannot have this bug.

Lesson 05 separates this from finding #3, because the two look identical from
outside and have completely different fixes. Measure exhaustively before
tuning.

---

## 5. The relevance floor admits some clearly-unrelated questions

**Severity: low — the prompt compensates**
**Found by:** [lesson 06](06-retrieval/) · **File:** [.env](../.env.example) / `RELEVANCE_FLOOR`

Measured against the real index, on 8 travel and 8 unrelated questions:

```
  travel questions    0.674 - 0.866
  unrelated questions 0.538 - 0.621

  at RELEVANCE_FLOOR=0.60:  8/8 travel kept, 1/8 junk admitted
                            ("what is the capital of Peru" = 0.621)
  at 0.65:                  8/8 travel kept, 0/8 junk admitted
  at 0.70:                  6/8 travel kept  <- real questions refused
```

`config.py` is already honest that the floor is "deliberately a COARSE guard"
and that the distributions overlap, and showing the score to the model is the
right compensation. Worth knowing that 0.65 looked slightly better on this
sample — but a 16-question sample is not a calibration. **Do not change this
without a proper eval set**, and note that finding #3's fix shifts the whole
distribution anyway.

---

## 6. `list_conversations` scans ~30× more rows than it needs

**Severity: low — works, does not scale**
**Found by:** [lesson 11](11-memory/) · **File:** [app/agent.py](../app/agent.py)

The checkpointer writes a checkpoint per **graph step**, not per turn — 16 for
one conversation in the real database. So listing conversations scans
`limit * 12` checkpoints and groups by thread in Python:

```
  checkpoints in data/conversations.sqlite3 : 16
  conversations                             :  1
  rows scanned to list 40 conversations     : 480  (~30x more than needed)
```

**Fix:** a small table of your own — `(session_id, title, updated_at,
turn_count)` — updated on each turn. Listing becomes one indexed query. The
checkpointer is a state store, not a conversation index, and the `× 12`
multiplier is chosen by feel.

---

## 7. `markdownify` accepts `heading_style="SETEXT"` and silently ignores it

**Severity: trivial — but a good trap to know**
**Found by:** [lesson 02](02-fetch-html/)

The valid values are `ATX`, `ATX_CLOSED` and `UNDERLINED`. "Setext" is the
standard name for the underlined style, so it is the natural thing to type —
and markdownify accepts it without complaint and does ATX anyway.

Nothing in NorthStar is broken by this (it correctly passes `ATX`), but it is
worth knowing *why* `ATX` matters: lesson 03's `MarkdownHeaderTextSplitter`
looks for `#` characters. With `UNDERLINED`, one test page dropped from 13
findable headings to 5, and the chunking strategy degrades silently toward
blind character splitting.

---

## 8. `GROQ_API_KEY` in your shell is shadowing the one in `.env`

**Severity: environmental — worth clearing**
**Found by:** [lesson 01](01-config/)

`pydantic-settings` gives real environment variables precedence over `.env`.
On this machine both are set, to **different values**. Both happen to be valid
keys right now, so nothing is broken — but editing `.env` will not change which
key the app uses, which is an hour-long debugging session waiting to happen.

`app/config.py`'s `dotenv_keys_shadowed_by_environment()` already detects and
reports this at startup and on `/health`. That function is doing its job; the
shell variable is worth clearing.

---

## Two non-findings worth recording

Received wisdom that **did not hold** when measured here. Both are the kind of
thing that gets repeated and shapes decisions.

**Embeddings did not fail on exact names.** The standard warning is that vector
search misses proper nouns and you need BM25. Against the real 1,298-chunk
index, five names — `Tian Tian Hainanese Chicken Rice`, `EZ-Link card`,
`Kinkaku-ji`, `Haw Par Villa`, `Fushimi Inari` — each ranked a literally
matching chunk at **#1**. Hybrid search is still worth adding for robustness on
rare terms, but the honest motivation is not a failure you can currently
observe. ([Lesson 04](04-embeddings/))

**FAISS is not buying measurable speed here.** 0.08 ms/query versus numpy's
0.38 ms, against a ~15 ms query embedding and a 1–3 second LLM call. Identical
results. FAISS is a fine default that costs nothing and scales — but at 1,298
vectors a numpy array would do, and FAISS's inability to filter is the direct
cause of finding #4. ([Lesson 05](05-vector-store/))

---

## And the limitation that shapes the most code

Embeddings **cannot hear negation**. Measured with this project's model:

```
  "open on Monday"          vs  "closed on Monday"           0.882
  "suitable for children"   vs  "not suitable for children"  0.810
  "cheap restaurants"       vs  "expensive restaurants"      0.800
  "indoor activities"       vs  "outdoor activities"         0.779
  "indoor activities"       vs  "museums and galleries"      0.692
```

Read the last two rows together: **the antonym scores higher than the correct
answer.** This is the concrete justification for `kb_tool.py`'s `categories`
argument being a hard metadata filter rather than a similarity search — a
keyword rule applied at ingest time fixes a limitation of a 33-million-parameter
neural network at query time.

Knowing which problems *not* to solve with the model is most of the
engineering. ([Lesson 04](04-embeddings/))
