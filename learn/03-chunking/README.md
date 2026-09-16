# 03 — Chunking: why split, and where to cut

**Real file:** [app/ingest/chunk.py](../../app/ingest/chunk.py)
**Library:** `langchain-text-splitters`

## Why split at all

Three reasons, in order of importance:

**1. An embedding is one fixed-size vector.** Lesson 04 turns text into 384
numbers. 384 numbers for a 188 KB document is a blurry average of everything it
mentions — Singapore's history, its airport, its hawker centres, its bus fares.
Search it for "how do I get from the airport" and you match the whole document
weakly. Split it into 200 pieces and one piece is *about* the airport.

**2. The model has a context limit — and a token budget.** You retrieve to
paste into a prompt. You cannot paste 188 KB. Groq's free tier here allows a
few thousand tokens per minute.

**3. Precision of citation.** "This came from the Singapore guide" is not
useful. "This came from *Singapore/Bugis > Eat > Budget*" is.

## The actual problem: where do you cut?

This is the whole lesson. Naively:

```python
chunks = [text[i:i+900] for i in range(0, len(text), 900)]
```

Run that on a travel guide and you get chunks like:

```
  ...Maxwell Food Centre, 1 Kadayanallur St. Open daily 8AM-2AM. The
  Tian Tian Hainanese Chicken Rice stall is
```

The chunk ends mid-sentence, mid-listing. The next chunk starts with
`famous and has queues...` and has no idea what is famous. **You have severed
the fact from its subject.** Retrieval will return one half or the other and
neither answers the question.

## NorthStar's answer: two-stage, structure-first

### Stage 1 — split on markdown headings

Travel guides are strongly sectioned: "Understand", "Get in", "Get around",
"See", "Eat", "Sleep". Those headings are *semantic boundaries the author
already drew for you*. Use them.

```python
MarkdownHeaderTextSplitter(headers_to_split_on=[("#","h1"),("##","h2"),("###","h3")])
```

Two things come out of this, and the second is easy to overlook:

- chunks that are **self-contained by construction** — an "Eat" section does
  not bleed into "Sleep"
- **a section path**: `h1 > h2 > h3` = `Wikivoyage: Singapore/Bugis > Eat >
  Budget`. This is what makes a citation readable, and it is free.

### Stage 2 — sub-split anything still too long

A "See" section can be 6 KB. So oversized sections go through a second
splitter, and *the separator list is the interesting part*:

```python
SUB_SPLIT_SEPARATORS = ["\n\n", "\n* ", "\n- ", "\n", ". ", " ", ""]
```

`RecursiveCharacterTextSplitter` tries these **in order**. Split on blank lines
if you can. If a piece is still too long, try list-item boundaries. Then
newlines, then sentences, then spaces, then — last resort — mid-word.

`"\n* "` and `"\n- "` are there for a specific reason: Wikivoyage POI listings
are bullet lines. A list-item boundary is a vastly better cut point than an
arbitrary space, because one bullet is one attraction with its address and
hours. **That list encodes domain knowledge about the corpus.** It is not a
generic setting.

That is the meaning of "recursive": recursively fall back to a worse separator
only when the better one leaves a piece too big.

## The `chunk_overlap` question

```
CHUNK_SIZE=900
CHUNK_OVERLAP=120
```

Overlap means consecutive chunks share their last/first 120 characters. Why
pay 13% more storage?

Because a cut always lands somewhere, and sometimes it lands in the middle of
the one sentence that answered the question. Overlap means a fact near a
boundary appears whole in at least one chunk. It is cheap insurance against a
problem you cannot predict.

Note the trade-off: overlap inflates your index and can return two chunks that
are 90% the same text, wasting prompt space on duplication. 10–20% of chunk
size is the usual range.

`app/config.py` validates `chunk_overlap < chunk_size` — see lesson 01. Set
overlap ≥ size and a naive splitter never advances.

## Metadata: the part that is not about splitting

After splitting, `chunk.py` attaches to every chunk:

| Field | Why |
|---|---|
| `source_title`, `source_url`, `publisher`, `license` | so `[S1]` can render as a link |
| `section_path` | the readable location |
| `destination` | **load-bearing** — see below |
| `categories` | **load-bearing** — see below |
| `chunk_id` | deduplicate when two searches return the same chunk |

### `destination` — refusing the wrong city

The knowledge base holds Singapore, Kyoto and Andhra Pradesh. Without a
destination on each chunk, a question about Kyoto can be answered from
Singapore's guide, because "best neighbourhoods for street food" embeds
similarly for both. That is a confidently-wrong answer, which is the exact
failure mode this app is built to avoid. Lesson 06.

### `categories` — and the one that does real work

Tags are assigned by keyword rules at ingest: `attractions`, `transport`,
`food`, `indoor`, `outdoor`, and so on.

`indoor`/`outdoor` are not decorative. They are what makes the flagship
scenario work:

> *"Will it rain Thursday, and what should I do if it does?"*
> weather tool says rain → model searches the KB with `categories=["indoor"]`

Without that tag at ingest time, there is no way to ask for wet-weather
alternatives at query time. **A decision made during ingestion determines what
the agent is capable of hours later.** That is the most important structural
idea in this lesson.

### A prepended section path, for the embedding's benefit

Each chunk's text begins with its own section path before being embedded. So
the vector for a chunk under *Bugis > Eat > Budget* contains the words "Bugis",
"Eat" and "Budget" — which means it matches "cheap food in Bugis" much better
than the bare body text would.

It also creates a wart: `kb_tool.py` has to strip that prefix back off before
showing the excerpt to the model, because the header already says it. Look for
`if body.startswith(section)` in
[app/rag/kb_tool.py](../../app/rag/kb_tool.py).

## Alternatives

| Strategy | How it cuts | Cost | When |
|---|---|---|---|
| fixed-size characters | every N chars | free | never, for prose |
| `RecursiveCharacterTextSplitter` | separator fallback list | free | **sane default for unstructured text** |
| `MarkdownHeaderTextSplitter` | on `#` headings | free | **used here** — markdown with real structure |
| `HTMLHeaderTextSplitter` | on HTML headings | free | skip the markdown conversion entirely |
| token-based (`tiktoken`) | N *tokens*, not chars | free | when you must hit an exact token budget |
| code splitters (AST) | function/class boundaries | free | indexing source code |
| **semantic chunking** | embed each sentence, cut where meaning shifts | one embedding pass per sentence | prose with no headings; genuinely better, genuinely slower |
| **propositional / LLM chunking** | an LLM rewrites text into standalone facts | an LLM call per document | highest quality, highest cost; good for dense reference material |
| parent-document retrieval | index small chunks, return their big parent | more storage | you want precise matching but wide context |
| late chunking | embed the whole doc, then pool per chunk | needs a long-context embedder | newer; keeps global context in each chunk vector |

**When would you switch?**

- **No headings in your corpus** (transcripts, chat logs, scraped PDFs) →
  heading splitting has nothing to work with. Try semantic chunking.
- **Answers need more context than one chunk** → parent-document retrieval.
- **Hard token ceiling** → token-based splitting; 900 characters is
  ~200–280 tokens, and that ratio varies with the language.

NorthStar's corpus is curated markdown with dense, reliable heading structure,
so the structural splitter is both the cheapest option and close to the best
one. That is a fact about *this corpus*, not a general truth.

## Run it

```bash
.venv/Scripts/python.exe learn/03-chunking/run.py
```

It chunks a real document four ways — naive characters, recursive characters,
heading-aware, and heading-aware + sub-split — and prints the actual cut
points so you can see facts being severed and then not severed. Then it shows
overlap, the category rules, and the real distribution across all 1,298 chunks
in `data/index/`.

## Next

[04 — Embeddings](../04-embeddings/) — the 384 numbers.
