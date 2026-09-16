"""Lesson 05 - build a vector store in numpy, then benchmark it vs FAISS.

    .venv/Scripts/python.exe learn/05-vector-store/run.py

Imports nothing from app/.
"""

import pickle
import time
import warnings
from collections import Counter
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
INDEX_DIR = ROOT / "data" / "index"
CACHE = ROOT / ".cache" / "fastembed"
MODEL = "BAAI/bge-small-en-v1.5"


def rule(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ======================================================================
rule("PART 1 - a complete vector store in 15 lines of numpy")
# ======================================================================

from fastembed import TextEmbedding

model = TextEmbedding(model_name=MODEL, cache_dir=str(CACHE))


def embed(texts: list[str]) -> np.ndarray:
    return np.array(list(model.embed(texts)))


class TinyVectorStore:
    """Everything a vector store fundamentally is. No dependencies but numpy."""

    def __init__(self) -> None:
        self.vectors: np.ndarray | None = None   # the numbers  (index.faiss)
        self.documents: list[dict] = []          # the text     (index.pkl)

    def add(self, texts: list[str], metadatas: list[dict]) -> None:
        new = embed(texts)
        self.vectors = new if self.vectors is None else np.vstack(
            [self.vectors, new]
        )
        self.documents.extend(
            {"text": t, "metadata": m} for t, m in zip(texts, metadatas)
        )

    def search(self, query: str, k: int = 3) -> list[tuple[float, dict]]:
        q = embed([query])[0]
        scores = self.vectors @ q            # <- the entire search
        best = np.argsort(-scores)[:k]
        return [(float(scores[i]), self.documents[i]) for i in best]


documents = [
    ("The MRT runs from 5:30AM to midnight. A single fare costs S$1-2.",
     {"section": "Get around", "tags": ["transport"]}),
    ("Maxwell Food Centre has 100 stalls. Try the chicken rice.",
     {"section": "Eat > Budget", "tags": ["food", "indoor"]}),
    ("Gardens by the Bay is an outdoor park with the Supertree Grove.",
     {"section": "See", "tags": ["attractions", "outdoor"]}),
    ("The National Museum is air-conditioned and open 10AM-7PM daily.",
     {"section": "See", "tags": ["attractions", "indoor"]}),
    ("Changi Airport connects to the city by MRT in about 45 minutes.",
     {"section": "Get in", "tags": ["transport"]}),
    ("Kinkaku-ji, the Golden Pavilion, is in northern Kyoto.",
     {"section": "See", "tags": ["attractions", "outdoor"]}),
]

store = TinyVectorStore()
store.add([t for t, _ in documents], [m for _, m in documents])

print(f"stored {len(store.documents)} documents")
print(f"vectors shape: {store.vectors.shape}   "
      f"({store.vectors.nbytes:,} bytes)")
print()
print("The search method, in full:")
print()
print("    q = embed([query])[0]")
print("    scores = self.vectors @ q        # (6, 384) @ (384,) -> (6,)")
print("    best = np.argsort(-scores)[:k]")
print()
print("That is a complete, CORRECT, EXACT vector search. Everything the")
print("vector-database industry sells is built on top of that line.")

for query in ("how do I get around the city",
              "somewhere to eat cheaply",
              "indoor things to do"):
    print()
    print(f"  query: {query!r}")
    for score, doc in store.search(query, k=3):
        print(f"    {score:.3f}  [{doc['metadata']['section']:<14}] "
              f"{doc['text'][:52]}")


# ======================================================================
rule("PART 2 - metadata filtering, and the over-fetch problem")
# ======================================================================

print("A vector store that only does similarity cannot answer")
print("'indoor things to do, and only in Singapore'.")
print()
print("FAISS IndexFlatIP cannot filter. So app/rag/retriever.py")
print("OVER-FETCHES and filters in python:")
print()
print("    fetch_k = k * 6 if (wanted or place) else k")
print("    results = store.similarity_search(query, k=fetch_k)")
print("    # ...then drop anything with the wrong destination/categories")
print()


def search_filtered(query: str, k: int = 2, tag: str | None = None,
                    overfetch: int = 6) -> list:
    """The real strategy: fetch k*6, then filter."""
    candidates = store.search(query, k=k * overfetch if tag else k)
    kept = []
    for score, doc in candidates:
        if tag and tag not in doc["metadata"]["tags"]:
            continue
        kept.append((score, doc))
        if len(kept) == k:
            break
    return kept, len(candidates)


for tag in (None, "indoor", "outdoor"):
    results, examined = search_filtered("things to do", k=2, tag=tag)
    print(f"  tag={tag!r:<10} examined {examined:>2} candidates, "
          f"kept {len(results)}")
    for score, doc in results:
        print(f"      {score:.3f}  {doc['text'][:56]}")

print()
print("Why over-fetch instead of filtering inside the index? The comment in")
print("retriever.py defends it, correctly:")
print("  - scores stay comparable between filtered and unfiltered searches")
print("  - no dependence on store-specific filter semantics")
print()
print("But it has a real failure mode. Demonstrated next.")


# ======================================================================
rule("PART 3 - the real index: FAISS vs brute-force numpy")
# ======================================================================

faiss_path = INDEX_DIR / "index.faiss"
pkl_path = INDEX_DIR / "index.pkl"

if not (faiss_path.is_file() and pkl_path.is_file()):
    print("no index on disk - run: python -m app.ingest.build_index")
else:
    import faiss

    index = faiss.read_index(str(faiss_path))
    docstore, mapping = pickle.loads(pkl_path.read_bytes())

    print("what is actually on disk:")
    print(f"  index.faiss  {faiss_path.stat().st_size / 1e6:>6.2f} MB   "
          f"the {index.ntotal} x {index.d} float32 matrix")
    print(f"  index.pkl    {pkl_path.stat().st_size / 1e6:>6.2f} MB   "
          f"the documents: text + metadata")
    print()
    print(f"  index class : {type(index).__name__}")
    print("                ^ 'Flat' = NO approximation. It compares against")
    print("                  every vector, exactly like the numpy line above.")
    print()
    print(f"  expected matrix size: {index.ntotal} x {index.d} x 4 bytes = "
          f"{index.ntotal * index.d * 4 / 1e6:.2f} MB")
    print("                        ^ matches index.faiss. It really is")
    print("                          just the raw floats plus a header.")

    # ---- FAISS only returns integers ---------------------------------
    print()
    print("-" * 70)
    print("FAISS only knows numbers")
    print("-" * 70)

    q = embed(["how do I pay for the metro"])[0].astype("float32")
    scores, ids = index.search(q.reshape(1, -1), 3)
    print(f"  index.search(...) returned ids: {[int(i) for i in ids[0]]}")
    print("  That is all it gives you. Row indices.")
    print()
    print("  index.pkl turns those into documents:")
    for rank, i in enumerate(ids[0]):
        doc = docstore._dict[mapping[int(i)]]
        print(f"    id {int(i):<5} -> {doc.metadata['section_path'][:56]}")
    print()
    print("  That mapping is the whole value langchain_community's FAISS")
    print("  wrapper adds: a docstore + an index_to_docstore_id dict.")

    # ---- the benchmark ------------------------------------------------
    print()
    print("-" * 70)
    print("BENCHMARK - is FAISS actually worth it at 1,298 vectors?")
    print("-" * 70)

    all_vectors = np.vstack(
        [index.reconstruct(i) for i in range(index.ntotal)]
    )
    queries = embed([
        "cheap food", "how to get around", "indoor activities",
        "what to see in Kyoto", "currency and money", "temples and shrines",
        "nightlife and bars", "family activities", "airport transfer",
        "walking trails",
    ]).astype("float32")

    RUNS = 200

    start = time.perf_counter()
    for _ in range(RUNS):
        for qv in queries:
            index.search(qv.reshape(1, -1), 5)
    faiss_ms = (time.perf_counter() - start) / (RUNS * len(queries)) * 1000

    start = time.perf_counter()
    for _ in range(RUNS):
        for qv in queries:
            s = all_vectors @ qv
            np.argpartition(-s, 5)[:5]
    numpy_ms = (time.perf_counter() - start) / (RUNS * len(queries)) * 1000

    print(f"  {'method':<34} {'ms / query':>12}")
    print(f"  {'-' * 34} {'-' * 12}")
    print(f"  {'FAISS IndexFlatIP':<34} {faiss_ms:>12.4f}")
    print(f"  {'numpy  all_vectors @ q':<34} {numpy_ms:>12.4f}")
    print()
    ratio = numpy_ms / faiss_ms if faiss_ms else 0
    print(f"  FAISS is {ratio:.1f}x the speed of a numpy one-liner.")
    print()
    print("  Both are far below the ~15 ms it takes to EMBED the query, and")
    print("  nowhere near the ~1-3 SECONDS the LLM call takes. At this scale")
    print("  the vector store is not the bottleneck and could not be.")
    print()
    print("  Honest conclusion: for 1,298 chunks a numpy array would do.")
    print("  FAISS costs nothing to use and scales, so it is a fine")
    print("  default - but it is not buying speed you can feel.")

    # ---- where FAISS DOES matter -------------------------------------
    print()
    print("-" * 70)
    print("Where FAISS DOES matter: approximation")
    print("-" * 70)
    print("""
  The reason FAISS exists is the ANN index types we do NOT use:

    IndexFlatIP   compare to all            100% recall   baseline
    IndexIVFFlat  cluster, search nearest   ~95% recall   ~50x faster
    IndexHNSW     small-world graph         ~98% recall   ~100x faster
    IndexIVFPQ    cluster + compress        ~85% recall   ~1000x faster

  ANN = approximate nearest neighbour = you accept occasionally
  missing the true best match in exchange for speed.

  At 1,298 vectors that trade is pure loss: you would give up
  correctness to save microseconds. Choosing Flat here is right.
  The crossover is somewhere around 10^5-10^6 vectors.
""")

    # ---- the filtering failure mode ----------------------------------
    print("-" * 70)
    print("THE OVER-FETCH FAILURE MODE, with real numbers")
    print("-" * 70)

    docs = [docstore._dict[mapping[i]] for i in range(index.ntotal)]
    per_place = Counter(d.metadata.get("destination", "?") for d in docs)
    print()
    print("  index composition:")
    for place, count in per_place.most_common():
        print(f"    {place:<18} {count:>5} chunks  "
              f"({count / len(docs):>5.1%})")

    K = 5
    FLOOR = 0.60          # settings.relevance_floor
    OVERFETCH = 6         # retriever.py: fetch_k = k * 6

    def usable(query: str, place: str, fetch_k: int) -> int:
        """Chunks that survive BOTH the destination filter and the floor."""
        qv = embed([query])[0].astype("float32")
        scores, ids = index.search(qv.reshape(1, -1), fetch_k)
        return sum(
            1 for rank, i in enumerate(ids[0])
            if docs[int(i)].metadata.get("destination") == place
            and float(scores[0][rank]) >= FLOOR
        )

    # ---- first, be fair: the normal case works -----------------------
    print()
    print("  FIRST, the case that works. When the query TEXT names the")
    print("  destination, that destination's chunks dominate the top 30:")
    print()
    for query in ("what to see in Andhra Pradesh",
                  "tourism in Andhra Pradesh",
                  "Andhra Pradesh temples"):
        found = usable(query, "Andhra Pradesh", K * OVERFETCH)
        print(f"    {found:>2}/{K} usable   {query!r}")
    print()
    print("  So the over-fetch strategy is not broken in general. Good.")

    # ---- now the case that does not ----------------------------------
    print()
    print("  NOW a realistic MULTI-TURN case. In conversation the user has")
    print("  already said where they are going, so turn 3 is just:")
    print()
    print('      user  : "where should I eat?"')
    print()
    print("  The model passes destination='Kyoto' as an ARGUMENT, but the")
    print("  query text carries no destination signal at all. And 71% of")
    print("  the index is Singapore:")
    print()
    print(f"    {'destination':<16} {'query':<24} {'usable':>8}")
    print(f"    {'-' * 16} {'-' * 24} {'-' * 8}")
    failures = 0
    for place in ("Kyoto", "Andhra Pradesh"):
        for query in ("where should I eat", "how do I get around",
                      "what should I see", "indoor activities"):
            found = usable(query, place, K * OVERFETCH)
            flag = "" if found >= K else "  <-- short"
            if found < K:
                failures += 1
            print(f"    {place:<16} {query!r:<24} {found:>4}/{K}{flag}")

    print()
    print(f"  {failures} of 8 come back short. But WHY? There are two")
    print("  possible culprits and they need separating, because they have")
    print("  completely different fixes:")
    print()
    print("    (a) over-fetch truncation - the chunks exist above the floor")
    print("        but never make the top-30 window")
    print("    (b) the relevance floor - the chunks simply do not score")
    print("        0.60 against this query, so no window size would help")
    print()
    print("  Measured exhaustively against all 1,298 vectors:")
    print()
    print(f"    {'destination':<16} {'query':<22} {'exist':>6} {'top30':>6} "
          f"{'cause':>12}")
    print(f"    {'-' * 16} {'-' * 22} {'-' * 6} {'-' * 6} {'-' * 12}")

    for place in ("Kyoto", "Andhra Pradesh"):
        mask = np.array(
            [d.metadata.get("destination") == place for d in docs]
        )
        for query in ("where should I eat", "what should I see",
                      "indoor activities"):
            qv = embed([query])[0]
            exhaustive = all_vectors @ qv
            exist = int((exhaustive[mask] >= FLOOR).sum())
            found = usable(query, place, K * OVERFETCH)
            if exist >= K and found < K:
                cause = "(a) fetch_k"
            elif exist < K:
                cause = "(b) floor"
            else:
                cause = "ok"
            print(f"    {place:<16} {query!r:<22} {exist:>6} {found:>6} "
                  f"{cause:>12}")

    print()
    print("  Both bugs are real and they are different:")
    print()
    print("  (a) Kyoto / 'where should I eat': 20 chunks clear the floor,")
    print("      only 3 reach the top-30 window. This IS the over-fetch")
    print("      bug. A bigger fetch_k fixes it; filtering inside the index")
    print("      fixes it properly:")
    print()
    for multiplier in (6, 12, 30, 60, 120):
        found = usable("where should I eat", "Kyoto", K * multiplier)
        print(f"        fetch_k = {K}*{multiplier:<4} = {K * multiplier:<5} "
              f"-> {found}/{K} usable Kyoto chunks")
    print()
    print("      Note it takes a much larger multiplier than 6 to recover")
    print("      all 5. Over-fetching harder 'works' but it is a race")
    print("      between the multiplier and how unbalanced the index is,")
    print("      and it gets slower as it gets more correct.")
    print()
    print("  (b) Andhra Pradesh / anything: ZERO chunks clear the floor.")
    print("      Best score is ~0.53 against a 0.60 floor. No fetch_k on")
    print("      earth helps. Two separate reasons, both worth knowing:")
    print("        - short generic queries score low against everything")
    print("          (lesson 04, step 4)")
    print("        - the Andhra Pradesh document is the junk one from")
    print("          lesson 03 - full of Wikipedia nav menus - so it")
    print("          embeds badly")
    print("      Lesson 06 takes the floor apart properly.")
    print()
    print("  THE TRANSFERABLE LESSON: 'retrieval returned nothing' has")
    print("  several possible causes that look identical from the outside.")
    print("  Always measure exhaustively before tuning a knob.")
    print()
    print("  For (a): Qdrant, Chroma and pgvector apply the filter INSIDE")
    print("  the index, always return k matching results, and cannot have")
    print("  this bug. ** That - not vector count - is the real argument")
    print("  for moving off FAISS here. **")

    # ---- pickle warning ----------------------------------------------
    print()
    print("-" * 70)
    print("One warning about index.pkl")
    print("-" * 70)
    print("""
  index.pkl is a python pickle: arbitrary bytecode. Loading an
  untrusted pickle EXECUTES CODE. Fine for a file your own pipeline
  wrote, which is why langchain's loader makes you write

      FAISS.load_local(..., allow_dangerous_deserialization=True)

  an ugly flag that is correctly ugly. If you ever accept a
  pre-built index from elsewhere, do not use pickle for it.
""")

print()
print("Next: learn/06-retrieval/README.md")
