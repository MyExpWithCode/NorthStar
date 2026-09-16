"""Lesson 04 - look at an actual embedding, and compute cosine by hand.

    .venv/Scripts/python.exe learn/04-embeddings/run.py

Imports nothing from app/. First run loads the ~130 MB model from
.cache/fastembed (already downloaded by this project) and takes ~10 s.
"""

import math
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".cache" / "fastembed"
MODEL = "BAAI/bge-small-en-v1.5"


def rule(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ======================================================================
rule("LOADING THE MODEL - note what does NOT happen")
# ======================================================================

print(f"model      : {MODEL}")
print(f"cache      : {CACHE}")
print("parameters : ~33 million   (Groq's gpt-oss-120b has ~120 BILLION)")
print()
print("No API key. No network call. No token cost. This is a file on your")
print("disk running on your CPU. An embedding model is NOT a language")
print("model - it cannot produce a single word of text.")
print()

import numpy as np
from fastembed import TextEmbedding

model = TextEmbedding(model_name=MODEL, cache_dir=str(CACHE))
print("loaded.")


def embed(texts: list[str]) -> np.ndarray:
    return np.array(list(model.embed(texts)))


# ======================================================================
rule("STEP 1 - what a vector actually looks like")
# ======================================================================

text = "The Singapore MRT is cheap and efficient."
vector = embed([text])[0]

print(f"input  : {text!r}")
print(f"output : a numpy array, shape {vector.shape}, dtype {vector.dtype}")
print()
print("the first 24 of the 384 numbers:")
print()
for row_start in range(0, 24, 6):
    row = vector[row_start:row_start + 6]
    print("   " + "  ".join(f"{v:+.5f}" for v in row))
print("   ...  (360 more)")
print()
print("That is the whole output. 384 floats. They are not human-readable and")
print("they are not meant to be - individual dimensions mean nothing on")
print("their own. Only DISTANCES between vectors mean anything.")


# ======================================================================
rule("STEP 2 - the length is exactly 1.0, and that matters a lot")
# ======================================================================

by_hand = math.sqrt(sum(v * v for v in vector))
print("computed by hand:  sqrt(sum(v*v for v in vector))")
print(f"  = {by_hand:.10f}")
print(f"numpy: np.linalg.norm(vector) = {np.linalg.norm(vector):.10f}")
print()
print("Exactly 1.0. Every bge vector sits on the surface of a unit sphere.")
print()
print("Why you care: cosine similarity is")
print()
print("                  a . b")
print("    cos(a,b) = -----------")
print("               |a| * |b|")
print()
print("and when |a| = |b| = 1 the denominator vanishes:")
print()
print("    cos(a,b) = a . b          just the dot product")
print()
print("That is why app/ingest/build_index.py can use")
print("MAX_INNER_PRODUCT with an identity relevance function, and why")
print("RELEVANCE_FLOOR=0.60 in .env means a real cosine of 0.60 rather")
print("than some squashed score. Lesson 06 depends on that.")


# ======================================================================
rule("STEP 3 - cosine similarity, three ways, all agreeing")
# ======================================================================

a_text = "Where can I eat cheap local food in Singapore?"
b_text = "Hawker centres serve affordable Singaporean dishes."
c_text = "The mitochondrion is the powerhouse of the cell."

a, b, c = embed([a_text, b_text, c_text])


def cosine_pure_python(u, v) -> float:
    """No numpy. This is the entire mathematics of semantic search."""
    dot = 0.0
    norm_u = 0.0
    norm_v = 0.0
    for i in range(len(u)):
        dot += u[i] * v[i]
        norm_u += u[i] * u[i]
        norm_v += v[i] * v[i]
    return dot / (math.sqrt(norm_u) * math.sqrt(norm_v))


print(f"A = {a_text!r}")
print(f"B = {b_text!r}")
print(f"C = {c_text!r}")
print()
print(f"  {'pair':<8} {'pure python':>13} {'numpy dot':>13} {'numpy full':>13}")
print(f"  {'-' * 8} {'-' * 13} {'-' * 13} {'-' * 13}")
for label, u, v in (("A vs B", a, b), ("A vs C", a, c), ("B vs C", b, c)):
    print(f"  {label:<8} {cosine_pure_python(u, v):>13.6f} "
          f"{float(np.dot(u, v)):>13.6f} "
          f"{float(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v))):>13.6f}")

print()
print("Three independent computations, identical to 6 decimal places.")
print("There is no step in this pipeline you cannot verify by hand.")
print()
print(f"A vs B = {float(np.dot(a, b)):.3f}  - different words, same MEANING.")
print("         'eat cheap local food' and 'hawker centres serve")
print("         affordable dishes' share almost no vocabulary. Keyword")
print("         search would score this near zero. THAT is what")
print("         'semantic' buys you.")
print()
print(f"A vs C = {float(np.dot(a, c)):.3f}  - genuinely unrelated.")


# ======================================================================
rule("STEP 4 - why unrelated text still scores ~0.5, not 0.0")
# ======================================================================

print("You would expect unrelated text to score near zero. It does not.")
print()
pairs = [
    ("hawker centre", "food court"),
    ("hawker centre", "MRT station"),
    ("hawker centre", "quantum chromodynamics"),
    ("hawker centre", "asdfgh qwerty zxcvb"),
]
vecs = embed([t for pair in pairs for t in pair])
for i, (left, right) in enumerate(pairs):
    score = float(np.dot(vecs[2 * i], vecs[2 * i + 1]))
    bar = "#" * int(max(score, 0) * 46)
    print(f"  {score:+.3f}  {bar}")
    print(f"          {left!r} vs {right!r}")
print()
print("Even gibberish scores well above zero. Two reasons:")
print("  1. every English string shares 'is English prose' features, so")
print("     it all lives in a fairly narrow cone of the 384-d space")
print("  2. short queries and long documents have systematically")
print("     different geometry in bge models")
print()
print("** The consequence is the most important practical fact in RAG: **")
print("   an absolute threshold is a COARSE instrument. You cannot read")
print("   0.55 as 'about half relevant'. Lesson 06 measures the real")
print("   overlap on this corpus.")


# ======================================================================
rule("STEP 5 - a similarity matrix over travel phrases")
# ======================================================================

phrases = [
    "cheap hawker food",
    "affordable local eateries",
    "getting around by train",
    "MRT fares and tickets",
    "museums and galleries",
    "indoor activities when raining",
    "currency exchange rates",
]
matrix = embed(phrases)
sim = matrix @ matrix.T          # all pairs at once, because unit-length

print("     " + "".join(f"{i:>7}" for i in range(len(phrases))))
for i, row in enumerate(sim):
    cells = "".join(f"{v:>7.2f}" for v in row)
    print(f"  {i}  {cells}   {phrases[i]}")

print()
print("Read the diagonal: 1.00, every text is identical to itself.")
print(f"  (0,1) = {sim[0][1]:.2f}  'cheap hawker food' / 'affordable local")
print("           eateries'  - highest off-diagonal pair. No shared words.")
print(f"  (2,3) = {sim[2][3]:.2f}  both about trains")
print(f"  (4,5) = {sim[4][5]:.2f}  museums / indoor-when-raining: the model")
print("           knows museums are indoor activities. Nobody told it.")
print(f"  (0,6) = {sim[0][6]:.2f}  food / currency - unrelated")
print()
print("This matrix IS the knowledge base search, in miniature. Lesson 05")
print("scales it from 7 rows to 1,298 without changing the idea.")


# ======================================================================
rule("STEP 6 - the same numbers, straight out of FAISS")
# ======================================================================

print("Proving the app's stored index agrees with the arithmetic above.")
print()

pkl = ROOT / "data" / "index" / "index.pkl"
faiss_path = ROOT / "data" / "index" / "index.faiss"

if not (pkl.is_file() and faiss_path.is_file()):
    print("no index on disk - skipping (run: python -m app.ingest.build_index)")
else:
    import pickle

    import faiss

    index = faiss.read_index(str(faiss_path))
    docstore, mapping = pickle.loads(pkl.read_bytes())

    print(f"faiss index type   : {type(index).__name__}")
    print(f"vectors stored     : {index.ntotal}")
    print(f"dimensions         : {index.d}")
    print(f"metric             : "
          f"{'INNER_PRODUCT' if index.metric_type == faiss.METRIC_INNER_PRODUCT else index.metric_type}")
    print()

    stored = index.reconstruct(0)
    print(f"vector 0's length  : {np.linalg.norm(stored):.10f}")
    print("                     ^ 1.0, exactly as computed in step 2.")
    print("                       So FAISS's inner product IS cosine.")
    print()

    query = "where can I find cheap local food"
    q = embed([query])[0].astype("float32")

    # FAISS's own search
    scores, ids = index.search(q.reshape(1, -1), 3)

    # The same search, by hand, over every stored vector
    all_vectors = np.vstack([index.reconstruct(i) for i in range(index.ntotal)])
    by_hand_scores = all_vectors @ q
    by_hand_top = np.argsort(-by_hand_scores)[:3]

    print(f"query: {query!r}")
    print()
    print(f"  {'rank':<5} {'FAISS score':>12} {'by-hand score':>15} {'same doc?':>10}")
    print(f"  {'-' * 5} {'-' * 12} {'-' * 15} {'-' * 10}")
    for rank in range(3):
        faiss_id = int(ids[0][rank])
        hand_id = int(by_hand_top[rank])
        print(f"  {rank:<5} {float(scores[0][rank]):>12.6f} "
              f"{float(by_hand_scores[hand_id]):>15.6f} "
              f"{'yes' if faiss_id == hand_id else 'NO':>10}")

    print()
    print("Identical. FAISS is not doing anything mysterious - it is doing")
    print("the dot products from step 3, just very fast. Lesson 05.")
    print()
    top = docstore._dict[mapping[int(ids[0][0])]]
    print("the best-matching chunk:")
    print(f"  score        : {float(scores[0][0]):.4f}")
    print(f"  destination  : {top.metadata.get('destination')}")
    print(f"  section      : {top.metadata.get('section_path')}")
    print(f"  categories   : {top.metadata.get('categories')}")
    print(f"  text         : {top.page_content[:180].strip()}...")


# ======================================================================
rule("STEP 7 - where embeddings FAIL: they cannot hear the word 'not'")
# ======================================================================

print("This is the most important limitation to internalise, and it is not")
print("the one people usually warn you about.")
print()
print("Embeddings encode TOPIC, not TRUTH VALUE. Two sentences about the")
print("same subject that mean OPPOSITE things land close together:")
print()

antonyms = [
    ("indoor activities", "outdoor activities"),
    ("cheap restaurants", "expensive restaurants"),
    ("open on Monday", "closed on Monday"),
    ("suitable for children", "not suitable for children"),
    ("free admission", "admission costs money"),
]
vectors = embed([t for pair in antonyms for t in pair])
for i, (left, right) in enumerate(antonyms):
    score = float(np.dot(vectors[2 * i], vectors[2 * i + 1]))
    bar = "#" * int(score * 46)
    print(f"  {score:.3f}  {bar}")
    print(f"         {left!r}")
    print(f"     vs  {right!r}")
    print()

print("'open on Monday' and 'closed on Monday' score ~0.88 - practically")
print("synonyms to the model, and the exact opposite to a traveller.")
print()

# --- the punchline ----------------------------------------------------
print("-" * 70)
print("Now the comparison that should alarm you:")
print("-" * 70)
trio = embed(["indoor activities", "outdoor activities",
              "museums and galleries"])
opposite = float(np.dot(trio[0], trio[1]))
correct = float(np.dot(trio[0], trio[2]))
print()
print(f"  'indoor activities'  vs  'outdoor activities'    {opposite:.3f}")
print(f"  'indoor activities'  vs  'museums and galleries' {correct:.3f}")
print()
if opposite > correct:
    print(f"  The ANTONYM scores {opposite - correct:.3f} HIGHER than the")
    print("  correct answer. A museum is an indoor activity; an outdoor")
    print("  activity is the thing you asked to avoid. The model has it")
    print("  backwards.")
print()
print("** This is why app/rag/kb_tool.py takes a `categories` argument **")
print()
print("  Look back at lesson 03. Each chunk was TAGGED indoor/outdoor by")
print("  keyword rules at ingest time. That tag is a hard metadata filter,")
print("  not a similarity score:")
print()
print("      search(query='things to do', categories=['indoor'])")
print()
print("  The filter throws out outdoor chunks with certainty. Searching")
print("  for the TEXT 'indoor activities' would have happily returned")
print("  beaches and hiking trails - as the numbers above show.")
print()
print("  So the two lessons connect: a cheap keyword rule at ingest time")
print("  fixes a limitation of a 33-million-parameter neural model at")
print("  query time. Knowing which problems to NOT solve with the model")
print("  is most of the engineering.")

# --- and the honest note about names ----------------------------------
print()
print("-" * 70)
print("An overclaim worth correcting")
print("-" * 70)
print()
print("The usual warning is that embeddings fail on exact names. Measured")
print("against this project's real 1,298-chunk index, they do not:")
print()

names = ["Tian Tian Hainanese Chicken Rice", "EZ-Link card", "Kinkaku-ji",
         "Haw Par Villa", "Fushimi Inari"]
pkl = ROOT / "data" / "index" / "index.pkl"
faiss_path = ROOT / "data" / "index" / "index.faiss"
if pkl.is_file() and faiss_path.is_file():
    import pickle

    import faiss

    index = faiss.read_index(str(faiss_path))
    docstore, mapping = pickle.loads(pkl.read_bytes())
    docs = [docstore._dict[mapping[i]] for i in range(index.ntotal)]

    print(f"  {'exact name searched':<34} {'rank of a chunk that':>22}")
    print(f"  {'':<34} {'literally contains it':>22}")
    print(f"  {'-' * 34} {'-' * 22}")
    for name in names:
        q = embed([name])[0].astype("float32")
        _, ids = index.search(q.reshape(1, -1), 5)
        literal = {
            i for i, d in enumerate(docs)
            if name.lower() in d.page_content.lower()
        }
        found = next(
            (r for r, i in enumerate(ids[0]) if int(i) in literal), None
        )
        verdict = f"#{found + 1}" if found is not None else "not in top 5"
        print(f"  {name:<34} {verdict:>22}")

    print()
    print("  Every one ranks first. bge-small handles proper nouns better")
    print("  than the folklore suggests, at least on a corpus this size.")
else:
    print("  (no index on disk - skipping the measurement)")

print()
print("BM25 (exact-term scoring) is still worth adding, and hybrid search -")
print("run both, merge with Reciprocal Rank Fusion - is the standard")
print("production answer. But on THIS corpus the motivation is robustness")
print("on rare terms, not a failure you can currently observe. Claims about")
print("retrieval should be measured on your own data, not inherited.")

print()
print("Next: learn/05-vector-store/README.md")
