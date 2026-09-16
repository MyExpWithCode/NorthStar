# 05 — Vector store: what FAISS does that a for-loop doesn't

**Real file:** [app/ingest/build_index.py](../../app/ingest/build_index.py)
**Libraries:** `faiss-cpu`, `langchain-community` (the `FAISS` wrapper)

## The honest answer first

For NorthStar's 1,298 chunks: **almost nothing.** A numpy one-liner is about as
fast. `run.py` measures it.

That is not a criticism — it is the useful thing to know. A vector store earns
its keep at a scale this app has not reached, and understanding *where* the
crossover is stops you cargo-culting a vector database into a project that
needs a dictionary.

## Brute-force search is one line

Search over unit-length vectors is a matrix multiply:

```python
scores = all_vectors @ query        # (1298, 384) @ (384,) -> (1298,)
top_k = np.argsort(-scores)[:5]
```

That is a complete, correct, exact vector search. 1,298 × 384 = 498,432
multiply-adds — microseconds in numpy. **This is what the whole "vector
database" category is built on top of.**

## So what is FAISS for?

Two distinct things, and they are usually conflated:

### 1. It is a well-optimised exact search (which is what we use)

`data/index/index.faiss` is an **`IndexFlatIP`**. "Flat" means no
approximation: it compares against every vector, exactly like the numpy line
above. "IP" means inner product.

What you get over numpy: SIMD-optimised C++, better cache behaviour, batched
queries, and a serialisation format. Real but modest gains at this size.

### 2. It can trade exactness for speed (which we do not use)

This is the actual reason FAISS exists. At 10 million vectors, brute force is
too slow, so you use an **approximate** index:

| Index type | Idea | Recall | Speed at 10M |
|---|---|---|---|
| `IndexFlatIP` | compare to all | 100% | slow (~seconds) |
| `IndexIVFFlat` | cluster first, search nearest clusters only | ~95% | ~50× faster |
| `IndexHNSW` | navigable small-world graph | ~98% | ~100× faster, more RAM |
| `IndexIVFPQ` | cluster + compress vectors | ~85% | ~1000× faster, tiny RAM |

**ANN — approximate nearest neighbour — means you accept occasionally missing
the true best match in exchange for speed.** For 1,298 chunks that trade is
pure loss: you would give up correctness to save microseconds. Choosing
`IndexFlatIP` here is the right call, and it is worth being able to say why.

## The bit that is genuinely not the vectors

A vector store is two data structures, and people forget the second:

```
data/index/
  index.faiss   2 MB   the 1298 x 384 float32 matrix
  index.pkl     1 MB   the DOCUMENTS: text + metadata, keyed by id
  manifest.json        what was built, when, with which model
```

FAISS only knows numbers. It returns **row indices** — `[847, 203, 1094]`.
Turning those into "the chunk from *Bugis > Eat > Budget* with this URL and
this licence" is entirely `index.pkl`'s job. That is what
`langchain_community`'s `FAISS` wrapper adds: a docstore plus an
`index_to_docstore_id` mapping.

`run.py` reads `index.pkl` directly with `pickle` so you can see there is no
magic in it.

**Pickle is worth one warning.** `index.pkl` is arbitrary Python bytecode;
loading an untrusted one executes code. Fine for a file your own pipeline
wrote, which is why `FAISS.load_local` requires
`allow_dangerous_deserialization=True` — an ugly flag that is correctly ugly.

## Two design decisions in `build_index.py` worth reading

### Cosine, not squashed distance

```python
FAISS.from_documents(
    chunks, embeddings,
    distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT,
    relevance_score_fn=_identity,
)
```

By default LangChain's `similarity_search_with_relevance_scores` applies a
normalising function to map distances into 0–1. With unit vectors and inner
product, the raw score *already is* cosine — so the identity function keeps it
interpretable. Without this, `RELEVANCE_FLOOR=0.60` would be a magic number
instead of a measurable one. Lesson 04 verified this holds.

### Atomic swap

The index is built into a temp directory and `shutil.move`d into place only
when complete. Two consequences:

- a chat request during a rebuild keeps being served by the **old** index
- a rebuild that crashes leaves the working index **untouched**, not
  half-written

Combined with `retriever.reload()` — which just drops the cached singleton so
the next search reloads from disk — this is how `/admin` can rebuild the index
without restarting the server or dropping a request. Simple, and the kind of
thing that is much harder to retrofit than to build in.

## Alternatives

| Store | Type | Runs where | When to pick it |
|---|---|---|---|
| **numpy array** | exact | in your process | **< ~50k vectors and no filtering.** Genuinely fine. |
| **FAISS** | exact or ANN | in your process | **used here.** Fast, local, zero infrastructure. No server, no filtering, no updates without rebuild |
| `sqlite-vec` / `sqlite-vss` | exact/ANN | a SQLite file | you already have SQLite and want SQL filtering alongside |
| **Chroma** | ANN | embedded or server | easiest "real" vector DB; metadata filtering built in |
| **Qdrant** | ANN (HNSW) | server / cloud | excellent filtering, hybrid search, Rust, good defaults |
| **pgvector** | exact/ANN | PostgreSQL | **if you already run Postgres, start here** — joins, transactions, backups solved |
| **Weaviate** | ANN | server / cloud | built-in hybrid search and modules |
| **Milvus** | ANN | cluster | billions of vectors, distributed |
| **Pinecone** | ANN | cloud only | fully managed, no ops, per-query cost |
| **Elasticsearch / OpenSearch** | both | cluster | you already run it and want BM25 + vectors together |
| **LanceDB** | ANN | embedded (files) | FAISS-like but with real filtering and versioning |

### The two limits that would push NorthStar off FAISS

**1. Filtering.** Look at `app/rag/retriever.py`:

```python
fetch_k = k * 6 if (wanted or place) else k
results = get_store().similarity_search_with_relevance_scores(query, k=fetch_k)
# ...then filter by destination and categories in Python
```

It over-fetches 6× and filters afterwards, because `IndexFlatIP` cannot filter.
There is a comment defending this — it keeps scores comparable between filtered
and unfiltered searches, and avoids store-specific filter semantics — and that
reasoning is sound.

But it is a workaround with a measurable failure mode, and `run.py` measures
it. The index is 71% Singapore / 27% Kyoto / 2% Andhra Pradesh. In a
conversational follow-up the user has already said where they are going, so the
query text carries no destination signal:

```
  destination=Kyoto, query="where should I eat"
      chunks above the floor, index-wide : 20
      chunks delivered with fetch_k=30   :  3
```

Seventeen perfectly good Kyoto chunks exist and are silently dropped, because
Singapore's 926 chunks crowd them out of the top-30 window. `fetch_k = k*12`
recovers all 5; `k*6` does not.

**Be careful attributing blame here, though.** `run.py` separates two causes
that look identical from outside — over-fetch truncation *(chunks clear the
floor but miss the window)* versus the relevance floor *(chunks never clear
0.60 at all)*. Of the eight follow-up searches it tests, only one is genuinely
the over-fetch bug; the rest are the floor, which is lesson 06's subject. That
distinction matters because the two have completely different fixes, and
tuning the wrong knob is the default outcome.

Qdrant, Chroma and pgvector apply the filter inside the index, always return
`k` matching results, and cannot have the over-fetch bug at all.

**2. Incremental updates.** Adding one document currently rebuilds all 1,298
vectors. Fine at 30 seconds; not fine at 30 minutes.

Either of those, not vector count, is what would actually motivate a move.

## Run it

```bash
.venv/Scripts/python.exe learn/05-vector-store/run.py
```

It builds a vector index from scratch in numpy (~15 lines), searches it,
verifies the result against FAISS, then **benchmarks brute-force numpy against
FAISS on the real 1,298-vector index** so you can see the difference for
yourself. It also opens `index.pkl` to show the docstore, and demonstrates the
over-fetch filtering problem with real numbers — including separating it from
the relevance floor, which is the more common cause and the one you would
otherwise misdiagnose.

## Next

[06 — Retrieval](../06-retrieval/) — the grounding guard.
