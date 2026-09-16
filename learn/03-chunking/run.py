"""Lesson 03 - four ways to cut a document, with the cut points shown.

    .venv/Scripts/python.exe learn/03-chunking/run.py

Imports nothing from app/. Rebuilds app/ingest/chunk.py's strategy from
scratch, then compares it against the real 1,298-chunk index on disk.
"""

import pickle
import re
import warnings
from collections import Counter
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "data" / "kb" / "wikivoyage-singapore-bugis.md"

CHUNK_SIZE = 900
CHUNK_OVERLAP = 120


def rule(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4:].lstrip() if end != -1 else text


if not DOC.is_file():
    candidates = sorted((ROOT / "data" / "kb").glob("wikivoyage-singapore-*.md"))
    DOC = candidates[0] if candidates else None

if DOC is None:
    raise SystemExit("no documents in data/kb/ - run the fetcher first")

body = strip_frontmatter(DOC.read_text(encoding="utf-8"))

print(f"document : {DOC.name}")
print(f"length   : {len(body):,} characters")
print(f"headings : {len(re.findall(r'^#+ ', body, re.M))}")
print(f"target chunk size: {CHUNK_SIZE}, overlap: {CHUNK_OVERLAP}")


# ======================================================================
rule("WAY 1 - fixed-size characters (the obvious wrong answer)")
# ======================================================================

naive = [body[i:i + CHUNK_SIZE] for i in range(0, len(body), CHUNK_SIZE)]
print(f"{len(naive)} chunks. One line of code. Look at the seams:")
print()

for i in range(min(3, len(naive) - 1)):
    print(f"  chunk {i} ENDS   ...{naive[i][-70:]!r}")
    print(f"  chunk {i+1} STARTS {naive[i+1][:70]!r}...")
    print()

print("  Every boundary lands mid-sentence. Worse, it lands mid-LISTING:")
print("  a Wikivoyage POI bullet carries a name, address, hours and price,")
print("  and this cuts it in half. Retrieval returns one half; neither half")
print("  answers the question. The fact has been severed from its subject.")

mid_word = sum(
    1 for i in range(len(naive) - 1)
    if naive[i] and naive[i][-1].isalnum() and naive[i + 1][:1].isalnum()
)
print()
print(f"  boundaries landing mid-WORD: {mid_word} of {len(naive) - 1}")


# ======================================================================
rule("WAY 2 - RecursiveCharacterTextSplitter (a real default)")
# ======================================================================

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

SEPARATORS = ["\n\n", "\n* ", "\n- ", "\n", ". ", " ", ""]

print("It tries separators IN ORDER, falling back only when a piece is")
print("still too big. That is what 'recursive' means here:")
print()
for i, sep in enumerate(SEPARATORS):
    label = {
        "\n\n": "blank line     (paragraph)",
        "\n* ": "list item      (a POI bullet!)",
        "\n- ": "list item      (other bullet style)",
        "\n": "any newline",
        ". ": "sentence end",
        " ": "any space",
        "": "mid-word       (last resort)",
    }[sep]
    print(f"  {i + 1}. {sep!r:<8} {label}")

print()
print("  '\\n* ' and '\\n- ' are NOT generic settings. They are there because")
print("  Wikivoyage POI listings are bullet lines. That list encodes domain")
print("  knowledge about THIS corpus.")

recursive = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    separators=SEPARATORS,
)
rec_chunks = recursive.split_text(body)
print()
print(f"{len(rec_chunks)} chunks. The same seams:")
print()
for i in range(min(3, len(rec_chunks) - 1)):
    print(f"  chunk {i} ENDS   ...{rec_chunks[i][-70:]!r}")
    print(f"  chunk {i+1} STARTS {rec_chunks[i+1][:70]!r}...")
    print()

mid_word_rec = sum(
    1 for i in range(len(rec_chunks) - 1)
    if rec_chunks[i] and rec_chunks[i][-1].isalnum() and rec_chunks[i + 1][:1].isalnum()
)
print(f"  boundaries landing mid-word: {mid_word_rec} of {len(rec_chunks) - 1}")
print()
print("  Much better. But notice what is STILL missing: none of these")
print("  chunks knows what section it came from. A citation can only say")
print("  'somewhere in the Bugis guide'.")


# ======================================================================
rule("WAY 3 - MarkdownHeaderTextSplitter (structure first)")
# ======================================================================

print("The author already drew the semantic boundaries. They are the")
print("headings. Use them.")
print()

header_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")],
    strip_headers=False,
)
sections = header_splitter.split_text(body)

print(f"{len(sections)} sections. And look what comes out for free:")
print()
print(f"  {'chars':>6}  {'h1 > h2 > h3':<44}")
print(f"  {'-' * 6}  {'-' * 44}")
for section in sections:
    path = " > ".join(
        section.metadata[k] for k in ("h1", "h2", "h3") if k in section.metadata
    )
    print(f"  {len(section.page_content):>6}  {path[:44]:<44}")

print()
print("  That right-hand column is the SECTION PATH. It cost nothing, and")
print("  it is the difference between a useless citation and a useful one:")
print()
print("    'somewhere in the Bugis guide'")
print("    'Wikivoyage: Singapore/Bugis > Eat > Budget'")
print()
oversized = [s for s in sections if len(s.page_content) > CHUNK_SIZE]
print(f"  But {len(oversized)} of {len(sections)} sections are over "
      f"{CHUNK_SIZE} chars.")
print("  Headings alone are not enough. Hence stage 2.")


# ======================================================================
rule("WAY 4 - both stages, which is what NorthStar does")
# ======================================================================


def two_stage(markdown: str) -> list[dict]:
    """The real strategy, ~20 lines."""
    out: list[dict] = []
    for section in header_splitter.split_text(markdown):
        path = " > ".join(
            section.metadata[k] for k in ("h1", "h2", "h3")
            if k in section.metadata
        )
        text = section.page_content
        pieces = [text] if len(text) <= CHUNK_SIZE else recursive.split_text(text)
        for piece in pieces:
            if len(piece.strip()) < 60:      # MIN_CHUNK_CHARS in the real code
                continue
            out.append({
                "section_path": path,
                # The path is PREPENDED before embedding, so the vector
                # contains the words "Bugis", "Eat", "Budget".
                "text": f"{path}\n\n{piece.strip()}" if path else piece.strip(),
                "body": piece.strip(),
            })
    return out


final = two_stage(body)
lengths = [len(c["body"]) for c in final]
print(f"{len(final)} chunks, every one carrying its section path.")
print()
print(f"  shortest : {min(lengths)} chars")
print(f"  longest  : {max(lengths)} chars")
print(f"  mean     : {sum(lengths) // len(lengths)} chars")
print()
print("How the four compare:")
print()
print(f"  {'strategy':<34} {'chunks':>7} {'mid-word cuts':>14} {'section path':>13}")
print(f"  {'-' * 34} {'-' * 7} {'-' * 14} {'-' * 13}")
print(f"  {'1 fixed characters':<34} {len(naive):>7} {mid_word:>14} {'no':>13}")
print(f"  {'2 recursive characters':<34} {len(rec_chunks):>7} "
      f"{mid_word_rec:>14} {'no':>13}")
print(f"  {'3 headings only':<34} {len(sections):>7} {0:>14} {'yes':>13}")
print(f"  {'4 headings + recursive (NorthStar)':<34} {len(final):>7} "
      f"{'~0':>14} {'yes':>13}")

print()
print("Three consecutive real chunks from strategy 4:")
for chunk in final[3:6]:
    print()
    print(f"  --- {chunk['section_path']} ---")
    print(f"  {chunk['body'][:200]}...")


# ======================================================================
rule("OVERLAP - why pay 13% more storage")
# ======================================================================

demo = ("Chinatown Complex Food Centre has over 200 stalls. "
        "The famous Hawker Chan sells soy sauce chicken rice for S$5. "
        "It opens at 10AM and queues form early. "
        "Maxwell Food Centre is a 5-minute walk away.")

print("First, the mechanic, hand-rolled in 3 lines so there is no mystery:")
print()
print("    step = chunk_size - chunk_overlap")
print("    chunks = [text[i:i+chunk_size] for i in range(0, len(text), step)]")
print()
print("  A sliding window. Consecutive chunks share `chunk_overlap`")
print("  characters. That is all overlap ever means.")

print()
print("Now why you would want it. One fact straddling a boundary:")
print()
print(f'  "{demo}"')
print()
for overlap in (0, 40):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=100, chunk_overlap=overlap, separators=[" ", ""]
    )
    print(f"  chunk_overlap={overlap}:")
    for i, piece in enumerate(splitter.split_text(demo)):
        print(f"    [{i}] {piece!r}")
    print()

print("  overlap=0  : chunk [1] starts 'rice for S$5. It opens at 10AM'.")
print("               Retrieve it alone and you know something opens at 10AM")
print("               and costs S$5, but not that it is Hawker Chan.")
print()
print("  overlap=40 : chunk [1] carries the whole fact - the name, the dish,")
print("               the price and the opening time together.")
print()
print("  The cost: a bigger index, and two retrieved chunks that are 90% the")
print("  same text - wasting prompt space on duplication. 10-20% of chunk")
print("  size is the usual compromise. NorthStar uses 120/900 = 13%.")

print()
print("  GOTCHA worth knowing. Run the same demo with separators=['. ', ...]")
print("  and overlap changes NOTHING. Why: overlap is applied while MERGING")
print("  small splits into a chunk. Split on '. ' and each sentence is")
print("  already ~50 chars against a 100-char target, so there is nothing to")
print("  merge and no overlap appears.")
print()
print("  So chunk_overlap interacts with your separator list. It is not an")
print("  independent knob, and setting it does not guarantee you get it.")


# ======================================================================
rule("CATEGORIES - the decision that decides what the agent CAN do")
# ======================================================================

INDOOR = ("museum", "gallery", "aquarium", "mall", "indoor", "air-conditioned",
          "theatre", "cinema", "temple", "mosque", "church", "library",
          "food court", "hawker centre", "casino", "exhibition", "sheltered")
OUTDOOR = ("park", "garden", "beach", "trail", "walking", "cycling", "hike",
           "island", "reservoir", "nature reserve", "boardwalk", "waterfront",
           "promenade", "zoo", "rooftop", "open-air", "outdoor", "quay")
MIN_KEYWORD_HITS = 2

HEADING_RULES = (
    (r"\bsee\b|sight|attraction|museum|galler", ("attractions",)),
    (r"\beat\b|\bdrink\b|food|hawker|restaurant", ("food",)),
    (r"get in|get around|transport|\bmrt\b|\bbus\b|taxi", ("transport",)),
    (r"\bbuy\b|shop|market|mall", ("shopping",)),
    (r"\bsleep\b|hotel|hostel|accommodation", ("accommodation",)),
    (r"understand|culture|religio|language|etiquette", ("culture",)),
)


def classify(section_path: str, text: str) -> list[str]:
    tags: set[str] = set()
    for pattern, names in HEADING_RULES:
        if re.search(pattern, section_path, re.IGNORECASE):
            tags.update(names)
    lowered = text.lower()
    # A chunk needs MIN_KEYWORD_HITS distinct matches, so one passing
    # mention of a park does not make a chunk "outdoor".
    if len({k for k in INDOOR if k in lowered}) >= MIN_KEYWORD_HITS:
        tags.add("indoor")
    if len({k for k in OUTDOOR if k in lowered}) >= MIN_KEYWORD_HITS:
        tags.add("outdoor")
    return sorted(tags)


print("Tags come from two signals: the SECTION PATH (regex) and the")
print("CHUNK TEXT (keyword counting).")
print()
# Every path starts with the same h1; drop it so the column is readable.
h1 = final[0]["section_path"] if final else ""
print(f"  section path  (h1 = {h1!r})")
print()
print(f"  {'> h2 > h3':<26} categories")
print(f"  {'-' * 26} {'-' * 30}")
for chunk in final:
    tail = chunk["section_path"][len(h1):].strip() or "(document root)"
    tags = classify(chunk["section_path"], chunk["body"])
    print(f"  {tail[:26]:<26} {', '.join(tags) or '(none)'}")

print()
print("  indoor/outdoor are NOT decorative. This is the flagship scenario:")
print()
print('    user  : "will it rain Thursday, and what should I do if it does?"')
print("    agent : calls the weather tool -> rain")
print('    agent : searches the KB with categories=["indoor"]')
print()
print("  Without that tag applied HERE, at ingest time, there is no way to")
print("  ask for wet-weather alternatives THERE, at query time.")
print()
print("  ** A decision made during ingestion determines what the agent is")
print("     capable of hours later. ** That is the most important structural")
print("     idea in this lesson.")


# ======================================================================
rule("THE REAL INDEX - all 1,298 chunks on disk")
# ======================================================================

pkl = ROOT / "data" / "index" / "index.pkl"
if not pkl.is_file():
    print("no index built yet")
else:
    # index.pkl is (docstore, index_to_docstore_id). Reading it directly
    # avoids needing the embedding model for this lesson.
    docstore, _ = pickle.loads(pkl.read_bytes())
    docs = list(docstore._dict.values())

    print(f"{len(docs)} chunks in data/index/")
    print()

    lengths = sorted(len(d.page_content) for d in docs)
    print("length distribution:")
    for label, value in (
        ("min", lengths[0]),
        ("p25", lengths[len(lengths) // 4]),
        ("median", lengths[len(lengths) // 2]),
        ("p75", lengths[3 * len(lengths) // 4]),
        ("max", lengths[-1]),
    ):
        bar = "#" * int(value / max(lengths) * 44)
        print(f"  {label:<7} {value:>6}  {bar}")

    print()
    print("per destination:")
    for place, count in Counter(
        d.metadata.get("destination", "?") for d in docs
    ).most_common():
        print(f"  {place:<20} {count:>5} chunks")

    print()
    print("category distribution:")
    tally: Counter = Counter()
    for d in docs:
        for category in d.metadata.get("categories") or []:
            tally[category] += 1
    for category, count in tally.most_common():
        bar = "#" * int(count / tally.most_common(1)[0][1] * 40)
        print(f"  {category:<16} {count:>5}  {bar}")

    untagged = [d for d in docs if not d.metadata.get("categories")]
    print()
    print(f"chunks with NO categories: {len(untagged)}")
    if untagged:
        print("  by source:")
        for source, count in Counter(
            d.metadata.get("source_id", "?") for d in untagged
        ).most_common():
            print(f"    {source:<32} {count:>4}")
        print()
        print("  An untagged chunk is still searchable, but it can never be")
        print("  found by a categories=['indoor'] filter. It is invisible to")
        print("  the flagship scenario.")

    # ---- a real defect, visible in the live index --------------------
    nav = [
        d for d in docs
        if "move to sidebar" in d.page_content or "Main menu" in d.page_content
    ]
    if nav:
        print()
        print("-" * 70)
        print("A REAL DEFECT, LIVE IN THIS INDEX")
        print("-" * 70)
        print(f"{len(nav)} chunk(s) contain Wikipedia navigation furniture:")
        for source, count in Counter(
            d.metadata.get("source_id", "?") for d in nav
        ).most_common():
            print(f"    {source:<32} {count:>4} chunks")
        print()
        print("  sample:")
        print(f"    {nav[0].page_content[:170]!r}")
        print()
        print("  Why: NorthStar has TWO ingestion paths.")
        print("    app/ingest/fetch_sources.py  32 remove-selectors + section")
        print("                                 dropping (curated sources)")
        print("    app/ingest/parsers.py        7 selectors: script, style,")
        print("                                 noscript, nav, header, footer,")
        print("                                 aside   (/admin uploads+URLs)")
        print()
        print("  A URL added through /admin takes the SECOND path, so")
        print("  Wikipedia's sidebar was never removed and got embedded as if")
        print("  it were travel content.")
        print()
        print("  This is lesson 02's warning made concrete: if the text you")
        print("  index is full of navigation menus, your search results will")
        print("  be full of navigation menus.")

print()
print("Next: learn/04-embeddings/README.md")
