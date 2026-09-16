"""Lesson 06 - the relevance floor, measured rather than asserted.

    .venv/Scripts/python.exe learn/06-retrieval/run.py

Imports nothing from app/. Re-implements the retrieval guards so you can
move the floor and watch what changes.
"""

import pickle
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
INDEX_DIR = ROOT / "data" / "index"
CACHE = ROOT / ".cache" / "fastembed"

FLOOR = 0.60          # settings.relevance_floor
K = 5                 # settings.retrieval_k
OVERFETCH = 6         # retriever.py: fetch_k = k * 6


def rule(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


if not (INDEX_DIR / "index.faiss").is_file():
    raise SystemExit("no index - run: python -m app.ingest.build_index")

import faiss
from fastembed import TextEmbedding

model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5", cache_dir=str(CACHE))
index = faiss.read_index(str(INDEX_DIR / "index.faiss"))
docstore, mapping = pickle.loads((INDEX_DIR / "index.pkl").read_bytes())
DOCS = [docstore._dict[mapping[i]] for i in range(index.ntotal)]
VECTORS = np.vstack([index.reconstruct(i) for i in range(index.ntotal)])

COVERED = sorted({d.metadata.get("destination", "") for d in DOCS} - {""})


def embed(texts: list[str]) -> np.ndarray:
    return np.array(list(model.embed(texts)))


def best_score(query: str, destination: str | None = None) -> float:
    q = embed([query])[0]
    scores = VECTORS @ q
    if destination:
        mask = np.array(
            [d.metadata.get("destination") == destination for d in DOCS]
        )
        scores = scores[mask]
    return float(scores.max()) if len(scores) else 0.0


# ======================================================================
rule("THE PROBLEM - why any of this exists")
# ======================================================================

print("""
  Ask a language model "what time does Bugis MRT station close?" and it
  answers. Confidently. With a plausible time. It may be wrong and you
  cannot tell from the answer.

  In a travel assistant that is not an abstract inaccuracy - someone
  stands outside a closed building.

  So the design goal is not "answer well". It is:

      ** "I don't know" must be a reachable outcome. **

  Everything below serves that one sentence.
""")
print(f"  knowledge base covers: {', '.join(COVERED)}")


# ======================================================================
rule("GUARD 2 - the relevance floor, and the overlap it cannot fix")
# ======================================================================

good = [
    "how do I get from Changi Airport to the city centre",
    "where can I eat cheap hawker food in Singapore",
    "what are the best neighbourhoods to explore in Singapore",
    "how much does the MRT cost and how do I pay",
    "what temples should I visit in Kyoto",
    "is it worth visiting Sentosa island",
    "what indoor attractions are there for a rainy day",
    "how many days do I need in Kyoto",
]
bad = [
    "how do I file my tax return",
    "what is the best programming language for web development",
    "explain the offside rule in football",
    "who won the 1998 world cup",
    "how do I fix a leaking kitchen tap",
    "what is the capital of Peru",
    "write me a poem about loneliness",
    "how does photosynthesis work",
]

print("Scoring real travel questions and clearly unrelated ones against")
print("the actual 1,298-chunk index. Best match per question:")
print()

good_scores = [(best_score(q), q) for q in good]
bad_scores = [(best_score(q), q) for q in bad]

print("  TRAVEL QUESTIONS (should be answered)")
for score, query in sorted(good_scores, reverse=True):
    bar = "#" * int(score * 50)
    mark = " " if score >= FLOOR else "X"
    print(f"   {mark} {score:.3f} {bar}")
    print(f"          {query}")

print()
print("  UNRELATED QUESTIONS (should be refused)")
for score, query in sorted(bad_scores, reverse=True):
    bar = "#" * int(score * 50)
    mark = "X" if score >= FLOOR else " "
    print(f"   {mark} {score:.3f} {bar}")
    print(f"          {query}")

g = [s for s, _ in good_scores]
b = [s for s, _ in bad_scores]
print()
print(f"  travel     : {min(g):.3f} - {max(g):.3f}   (mean {sum(g)/len(g):.3f})")
print(f"  unrelated  : {min(b):.3f} - {max(b):.3f}   (mean {sum(b)/len(b):.3f})")
print()

overlap_lo, overlap_hi = min(g), max(b)
if overlap_lo < overlap_hi:
    print(f"  ** THE RANGES OVERLAP between {overlap_lo:.3f} and "
          f"{overlap_hi:.3f}. **")
    print()
    print("  config.py says this itself and calls the floor 'deliberately")
    print("  a COARSE guard'. That honesty is the most instructive thing")
    print("  in the file: NO threshold can separate two overlapping")
    print("  distributions. There is no correct value of 0.60.")
else:
    print(f"  The ranges happen to separate cleanly on this sample")
    print(f"  (gap {overlap_lo - overlap_hi:.3f}), but config.py's wider")
    print("  calibration found them overlapping. A clean gap on 16")
    print("  questions is not evidence of a clean gap in general.")


# ======================================================================
rule("MOVING THE FLOOR - what each choice actually costs")
# ======================================================================

print(f"  {'floor':>6} {'travel kept':>13} {'unrelated kept':>16}  verdict")
print(f"  {'-' * 6} {'-' * 13} {'-' * 16}  {'-' * 28}")
for candidate in (0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75):
    kept_good = sum(1 for s in g if s >= candidate)
    kept_bad = sum(1 for s in b if s >= candidate)
    if kept_good == len(g) and kept_bad == 0:
        verdict = "perfect on THIS sample"
    elif kept_bad > 0 and kept_good == len(g):
        verdict = f"{kept_bad} junk question(s) admitted"
    elif kept_good < len(g) and kept_bad == 0:
        verdict = f"{len(g) - kept_good} real question(s) refused"
    else:
        verdict = "both kinds of error"
    marker = "  <-- shipped" if candidate == FLOOR else ""
    print(f"  {candidate:>6.2f} {kept_good:>8}/{len(g)} "
          f"{kept_bad:>11}/{len(b)}  {verdict}{marker}")

print()
print("  higher -> false 'I don't know' on real questions")
print("  lower  -> junk presented to the user as evidence")
print()
print("  Which error you prefer is a PRODUCT decision, not a tuning")
print("  exercise. This app prefers refusing, because a confident wrong")
print("  opening time is worse than an admitted gap.")


# ======================================================================
rule("GUARD 3 - show the model the score and let it judge")
# ======================================================================

print("Because the floor cannot be sufficient, kb_tool.py puts the score")
print("into the excerpt header the model reads:")
print()

query = "what indoor attractions are there for a rainy day"
q = embed([query])[0]
scores = VECTORS @ q
top = np.argsort(-scores)[:K]

for rank, i in enumerate(top, 1):
    doc = DOCS[int(i)]
    score = float(scores[int(i)])
    section = doc.metadata.get("section_path", "")
    body = doc.page_content.strip()
    if body.startswith(section):
        body = body[len(section):].strip()     # the real code does this too
    body = body[:200].replace("\n", " ")
    print(f"  [S{rank}] {section[:52]} (relevance {score:.2f})")
    print(f"       {body}...")
    print()

print("  A 0.61 match is SHOWN, marked as weak, and the model can say")
print("  'the guide mentions this only in passing'. The floor stops")
print("  garbage; the model handles marginal. Two different jobs.")


# ======================================================================
rule("GUARD 4 - two sentinels that must not be merged")
# ======================================================================


def kb_search(query: str, destination: str = "") -> tuple[str, list]:
    """A cut-down kb_tool.py. The guards, without the error handling."""
    if destination:
        resolved = next(
            (c for c in COVERED if c.lower() == destination.strip().lower()),
            None,
        )
        if resolved is None:
            return (
                "DESTINATION_NOT_COVERED\n"
                f"No documents about {destination!r}. Covers: "
                f"{', '.join(COVERED)}.\n"
                "Tell the user plainly; do not answer from general knowledge.",
                [],
            )
    else:
        resolved = None

    q = embed([query])[0]
    scores = VECTORS @ q
    order = np.argsort(-scores)[:K * OVERFETCH]

    hits = []
    for i in order:
        if float(scores[int(i)]) < FLOOR:
            continue
        if resolved and DOCS[int(i)].metadata.get("destination") != resolved:
            continue
        hits.append((float(scores[int(i)]), DOCS[int(i)]))
        if len(hits) == K:
            break

    if not hits:
        return (
            "NO_RELEVANT_CONTENT\n"
            f"Nothing about {resolved or 'anywhere'} above the relevance "
            f"threshold for: {query!r}\n"
            "Tell the user this topic is not covered.",
            [],
        )
    return f"{len(hits)} excerpt(s) returned", hits


cases = [
    ("what should I see", "Rome", "a place with NO documents"),
    ("how do I file my taxes", "Singapore", "covered place, uncovered topic"),
    ("what temples should I visit", "Kyoto", "should work"),
]
for query, destination, label in cases:
    content, hits = kb_search(query, destination)
    print(f"  query       : {query!r}")
    print(f"  destination : {destination!r}   ({label})")
    print(f"  ->            {content.splitlines()[0]}")
    if hits:
        print(f"                best score {hits[0][0]:.3f}, "
              f"{hits[0][1].metadata['section_path'][:44]}")
    print()

print("  Why two sentinels rather than one 'not found':")
print()
print("    DESTINATION_NOT_COVERED -> 'I have no guide for Rome.'")
print("    NO_RELEVANT_CONTENT     -> 'My Singapore guide omits that.'")
print()
print("  Different admissions. Merging them would let a Rome question be")
print("  answered from Singapore's guide - and lesson 04 showed the")
print("  vectors will not stop you, because 'best neighbourhood for")
print("  street food' embeds almost identically for any city. Only")
print("  metadata can enforce this.")


# ======================================================================
rule("THE MEASURED FAILURE - and whether query rewriting fixes it")
# ======================================================================

print("Lesson 05 found that short conversational follow-ups deliver too")
print("few chunks. The floor was calibrated on FULL questions; a")
print("fragment behaves nothing like one.")
print()


def kyoto_stats(query: str) -> tuple[float, float, int]:
    q = embed([query])[0]
    scores = (VECTORS @ q)[
        np.array([d.metadata.get("destination") == "Kyoto" for d in DOCS])
    ]
    return float(scores.max()), float(scores.mean()), int((scores >= FLOOR).sum())


total_kyoto = sum(1 for d in DOCS if d.metadata.get("destination") == "Kyoto")

print(f"  {'query':<50} {'best':>6} {'mean':>6} {'>=floor':>9}")
print(f"  {'-' * 50} {'-' * 6} {'-' * 6} {'-' * 9}")
for query in (
    "what should I see",
    "main sights, attractions and temples to see in Kyoto",
    "Kyoto",
):
    best, mean, count = kyoto_stats(query)
    print(f"  {query:<50} {best:>6.3f} {mean:>6.3f} "
          f"{count:>5}/{total_kyoto}")

print()
print("  Note the THIRD row. The bare word 'Kyoto' - no information need")
print("  at all - lifts nearly every Kyoto chunk above the floor.")
print()
print("  Why: lesson 03 PREPENDED each chunk's section path before")
print("  embedding, and every Kyoto section path starts 'Wikivoyage:")
print("  Kyoto/...'. So naming the destination in the query raises the")
print("  score of every chunk in that destination, uniformly.")
print()
print("  ** That means the 'chunks above the floor' count is a MISLEADING")
print("     metric, and my first attempt at this lesson used it. **")
print("     A rewrite that mentions the place appears to improve")
print("     retrieval by +300 chunks while telling you nothing.")

print()
print("-" * 70)
print("So measure RANKING instead of counting")
print("-" * 70)
print()

for query in ("what should I see",
              "main sights, attractions and temples to see in Kyoto"):
    q = embed([query])[0]
    scores = VECTORS @ q
    kyoto_only = np.where(
        np.array([d.metadata.get("destination") == "Kyoto" for d in DOCS]),
        scores, -9.0,
    )
    print(f"  {query!r}")
    for i in np.argsort(-kyoto_only)[:3]:
        print(f"    {scores[int(i)]:.3f}  "
              f"{DOCS[int(i)].metadata['section_path'][:56]}")
    print()

print("  NOW the improvement is real and legible. The raw fragment ranks")
print("  'Kyoto/Arashiyama > Get around' first - a TRANSPORT section, for")
print("  a question about sights. The rewritten query ranks")
print("  'Kyoto/Central > See' first, which is correct.")
print()
print("  So query rewriting genuinely helps - but it helps by improving")
print("  RANKING, not by lifting scores over a threshold. Those are")
print("  different mechanisms and only one of them is progress.")

print()
print("-" * 70)
print("An uncomfortable consequence for the floor")
print("-" * 70)
print("""
  If naming the destination pushes ~95% of that destination's chunks
  above 0.60, then for any query that mentions the place the floor
  stops discriminating almost entirely. Its protective value depends
  on how the model happens to phrase the query.

  That is a real design fragility, and it is not visible from
  reading retriever.py. It falls out of a decision made in
  chunk.py - prepending the section path - interacting with a
  decision made in config.py. Neither file mentions the other.

  This is what makes RAG pipelines hard to reason about: the
  behaviour lives in the INTERACTIONS, not in any one module.
""")

print("-" * 70)
print("And the case rewriting cannot fix")
print("-" * 70)
print()
ap_total = sum(1 for d in DOCS if d.metadata.get("destination") == "Andhra Pradesh")
print(f"  {'query':<52} {'best':>6}")
print(f"  {'-' * 52} {'-' * 6}")
for query in ("where should I eat",
              "where to eat in Andhra Pradesh: local food and restaurants",
              "Tirumala temple"):
    print(f"  {query:<52} {best_score(query, 'Andhra Pradesh'):>6.3f}")

print()
print(f"  Andhra Pradesh has only {ap_total} chunks, and its source")
print("  document is the junk one from lesson 03 - Wikipedia navigation")
print("  menus, 14 of 16 chunks untagged. Rewriting lifts the scores")
print("  (because the name appears in the path) but the CONTENT is still")
print("  nav furniture.")
print()
print("  ** Bad ingestion produces content that is technically")
print("     retrievable and practically useless. No query-time tuning")
print("     rescues it. The bug is four lessons upstream. **")


# ======================================================================
rule("SUMMARY - four causes of 'retrieval returned nothing'")
# ======================================================================
print("""
  They are indistinguishable from the outside, and they have
  completely different fixes:

    1. INGESTION quality   nav menus indexed as content   (lesson 02/03)
                           -> fix the parser

    2. CHUNKING            no section path, no categories  (lesson 03)
                           -> fix the splitter, re-tag

    3. QUERY PHRASING      short fragments score low       (lesson 04/06)
                           -> query rewriting / expansion

    4. THE THRESHOLD       floor set too high              (lesson 06)
                           -> measure both distributions first

  The instinct is always to reach for 4, because it is one number in
  a .env file. In this project 4 is the only one that is currently
  set correctly.

  ** Measure before you tune. **
""")

print("Next: learn/07-call-an-llm/README.md")
