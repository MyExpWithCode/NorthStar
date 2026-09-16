# 04 — Embeddings: what a vector actually is

**Real file:** [app/ingest/build_index.py](../../app/ingest/build_index.py) (`get_embeddings`)
**Library:** `fastembed` (ONNX runtime), model `BAAI/bge-small-en-v1.5`

This is the lesson that makes RAG stop being magic. Read it slowly.

## First: an embedding model is not a language model

This confuses almost everyone at the start, so let us be blunt.

| | Language model (Groq/Claude) | Embedding model (bge-small) |
|---|---|---|
| Input | text | text |
| Output | **text** | **384 numbers** |
| Size | 120 billion parameters | 33 million parameters |
| Where it runs | someone's datacentre | your laptop, CPU, ~50 ms |
| Cost | per token, needs an API key | free, no key, no network |
| Can it chat? | yes | **no. It cannot produce words at all.** |

`fastembed` never talks to a server. `.cache/fastembed/` holds a 130 MB file
that runs locally. There is no "AI" in lesson 04 in the sense you are used to —
this is a fixed mathematical function from text to numbers.

## What the numbers mean

`bge-small-en-v1.5` maps any text to a point in 384-dimensional space. You
cannot picture 384 dimensions, so picture 2:

```
                      ^ dim 2
                      |
      "hawker centre" *      * "food court"
                      |
                      |            * "MRT station"
        "temple" *    |          * "bus fare"
                      |
  --------------------+-------------------> dim 1
```

The model was trained so that **texts about similar things land near each
other**. Nobody programmed "hawker centre is like food court". It emerged from
training on hundreds of millions of text pairs.

That is the entire foundation of semantic search: if *meaning* becomes
*position*, then *similar meaning* becomes *nearby position*, and finding
relevant text becomes geometry.

## Measuring "near": cosine similarity

Two vectors, `a` and `b`. Cosine similarity is:

```
                a · b              sum(a[i] * b[i])
  cos(a, b) = ---------  =  --------------------------------
              |a| × |b|      sqrt(sum(a²)) × sqrt(sum(b²))
```

The dot product divided by both lengths. It measures the **angle** between
them, ignoring magnitude:

```
  cos =  1.0   same direction      -> same meaning
  cos =  0.7   fairly close        -> related
  cos =  0.0   perpendicular       -> unrelated
  cos = -1.0   opposite direction  -> (rare in practice, see below)
```

`run.py` computes this by hand with a `for` loop, then with numpy, then checks
both against what FAISS reports. All three agree to 1e-6. **There is no step in
this pipeline you cannot verify with arithmetic.**

### A shortcut that matters: unit-length vectors

bge models emit vectors whose length is already exactly 1.0. When `|a| = |b| =
1`, the formula collapses:

```
  cos(a, b) = a · b        just the dot product
```

That is why `build_index.py` can use `DistanceStrategy.MAX_INNER_PRODUCT` with
an identity relevance function:

```python
def _identity(score: float) -> float:
    return score          # the raw inner product already IS cosine
```

And that is why `RELEVANCE_FLOOR=0.60` in `.env` is a number you can reason
about. Many vector-store setups return a squashed, normalised "relevance score"
where 0.6 means nothing in particular. Here it means *the cosine similarity is
0.60*. Lesson 06 depends on that being true.

The comment in `build_index.py` is worth reading — `normalize_L2` is
deliberately **not** passed, because FAISS ignores it for inner-product indexes
and the vectors are already unit-length.

## The limitation that matters most: embeddings cannot hear "not"

This is the finding that changed how I'd read `app/rag/kb_tool.py`, and it is
not the limitation people usually warn about. Measured, with this model:

| pair | cosine |
|---|---|
| `"open on Monday"` vs `"closed on Monday"` | **0.882** |
| `"suitable for children"` vs `"not suitable for children"` | **0.810** |
| `"cheap restaurants"` vs `"expensive restaurants"` | **0.800** |
| `"indoor activities"` vs `"outdoor activities"` | **0.779** |
| `"indoor activities"` vs `"museums and galleries"` | 0.692 |

Read the last two rows together. **The antonym scores higher than the correct
answer.** "Outdoor activities" is closer to "indoor activities" than "museums
and galleries" is — even though a museum *is* an indoor activity and an outdoor
activity is precisely the thing you asked to avoid.

Embeddings encode *topic*, not *truth value*. Negation and antonymy are nearly
invisible to them, because opposites are talked about in the same contexts.

### This is why `kb_tool.py` has a `categories` argument

Go back to lesson 03: every chunk was tagged `indoor` or `outdoor` by keyword
rules at ingest time. That tag is a **hard metadata filter**, not a similarity
score:

```python
search(query="things to do", categories=["indoor"])
```

The filter excludes outdoor chunks with certainty. Searching for the *text*
"indoor activities" would cheerfully return beaches and hiking trails, as the
numbers above show.

So a cheap regex at ingest time fixes a limitation of a 33-million-parameter
neural network at query time. **Knowing which problems not to solve with the
model is most of the engineering.**

## Why scores don't go below ~0.3 in practice

You would expect unrelated text to score near 0. It does not — unrelated pairs
here score roughly 0.3–0.6, and even random gibberish against real text scores
~0.51. Two reasons:

1. Modern embedding models put all natural-language text in a fairly narrow
   cone of the space. Everything shares "is English prose" features.
2. bge models are trained with an instruction prefix convention, and short
   queries versus long documents have systematically different geometry.

**The practical consequence is important:** an absolute threshold is a *coarse*
instrument. On this corpus, real travel questions score 0.62–0.86 and unrelated
questions score 0.49–0.63. **Those ranges overlap.** No threshold can perfectly
separate them, which is exactly why `app/rag/kb_tool.py` sends the score to the
model and lets it judge, rather than trusting the floor alone. Lesson 06.

## Why `fastembed` and not `sentence-transformers`

`sentence-transformers` is the better-known library and runs the same models.
NorthStar uses `fastembed`, and the reason is a single word in
`build_index.py`'s docstring: **no torch.**

| | fastembed | sentence-transformers |
|---|---|---|
| Runtime | ONNX Runtime | PyTorch |
| Install size | ~50 MB | ~2.5 GB (torch + CUDA libs) |
| CPU speed | faster (ONNX is optimised for it) | fine |
| GPU | no | yes |
| Fine-tuning | no | yes |
| Model choice | ~20 curated | thousands (all of HuggingFace) |

For an app that embeds 1,298 chunks once and then embeds one short query per
search, on CPU: fastembed is smaller, faster and simpler. If you needed to
fine-tune an embedding model on travel text, that trade-off inverts completely.

## The asymmetry nobody mentions

Query embedding happens on **every search**, inside the request. Document
embedding happens **once**, offline.

This means embedding cost has two very different profiles, and it is why hosted
embedding APIs (OpenAI, Cohere, Voyage) can be a poor fit for interactive
search: you add a network round-trip to every single query. A local 33M-param
model takes ~15 ms. An API call takes 100–400 ms, and can fail.

## Alternatives

| Model / service | Dims | Local? | Notes |
|---|---|---|---|
| `bge-small-en-v1.5` | 384 | yes | **used here.** Best size/quality point for English |
| `bge-base-en-v1.5` | 768 | yes | ~3× slower, modestly better |
| `all-MiniLM-L6-v2` | 384 | yes | the old default; bge-small beats it |
| `nomic-embed-text-v1.5` | 768 | yes | 8k context, good for long chunks |
| `multilingual-e5` / `bge-m3` | 1024 | yes | **pick this if your corpus is not English** |
| OpenAI `text-embedding-3-small` | 1536 | no | strong, cheap, needs a key + network |
| Cohere `embed-v3` | 1024 | no | has a document/query mode distinction |
| Voyage `voyage-3` | 1024 | no | currently top of several leaderboards |
| **TF-IDF / BM25** | n/a | yes | **not semantic** — exact word matching. Still excellent, and often better than embeddings for names, codes and rare terms |

Two things to note about that table:

**Dimensions are not quality.** 1536 dims is not 4× better than 384. It is 4×
the storage and 4× the search cost. Check MTEB scores, not dimension counts.

**BM25 deserves real respect** — but check the usual argument for it against
your own data. The standard claim is that embeddings fail on exact names, so a
query for "Hawker Chan" scores below a chunk about hawker centres generally.
I measured that against this project's real index and **it is not true here**:
`Tian Tian Hainanese Chicken Rice`, `EZ-Link card`, `Kinkaku-ji`,
`Haw Par Villa` and `Fushimi Inari` each rank a literally-matching chunk at #1.
`bge-small` handles proper nouns better than the folklore suggests, at least at
this corpus size.

Hybrid search (run both, merge with Reciprocal Rank Fusion) is still the
standard production answer and still worth adding — but the honest motivation
here is *robustness on rare terms*, not a failure you can currently observe.
Retrieval claims should be measured on your data, not inherited.

## Run it

```bash
.venv/Scripts/python.exe learn/04-embeddings/run.py
```

It prints an actual vector, proves its length is 1.0, computes cosine three
ways (pure python `for` loop, numpy, and FAISS's own search) to show all three
agree to six decimal places, builds a similarity matrix over travel phrases,
demonstrates the antonym failure, and measures exact-name retrieval against the
real 1,298-chunk index.

First run takes ~10 s to load the model; after that it is fast.

## Next

[05 — Vector store](../05-vector-store/) — storing and searching them.
