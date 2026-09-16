"""Turn knowledge-base documents into retrievable, labelled chunks.

Two-stage split:

1. **Split on markdown headings** (`#`, `##`, `###`). Travel guides are strongly
   sectioned -- "See", "Eat", "Get around", "Itineraries" -- and a blind
   character split severs an attraction from its opening hours. Splitting on
   headings keeps a chunk self-contained and yields a readable section path
   such as `Wikivoyage: Singapore/Bugis > Eat > Budget`.
2. **Sub-split oversized sections** on paragraph and list-item boundaries, so a
   long "See" section becomes several chunks without cutting a POI listing in
   half.

Every chunk also carries its **destination**, so a question about a place
the knowledge base does not cover is refused rather than answered from another
city's guide.

Every chunk is then **tagged with categories**. The `indoor` / `outdoor` tags
are load-bearing rather than decorative: they are what lets the agent retrieve
indoor alternatives once the weather tool reports a wet day.

    python -m app.ingest.chunk --stats
    python -m app.ingest.chunk --category indoor --sample 3
    python -m app.ingest.chunk --destination Singapore --sample 2
"""

from __future__ import annotations

import argparse
import re
import statistics
from collections import Counter

from langchain_core.documents import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from app.config import settings
from app.ingest import frontmatter
from app.ingest.registry import SourceRecord, SourceRegistry

HEADERS_TO_SPLIT_ON = [("#", "h1"), ("##", "h2"), ("###", "h3")]
HEADER_KEYS = ("h1", "h2", "h3")

#: Prefer paragraph, then list-item, then line boundaries. Wikivoyage POI
#: listings are bullet lines, so a list-item boundary is a far better cut point
#: than an arbitrary space.
SUB_SPLIT_SEPARATORS = ["\n\n", "\n* ", "\n- ", "\n", ". ", " ", ""]

#: Chunks shorter than this carry no retrievable meaning -- stray breadcrumbs,
#: or empty sections left behind by removed widgets.
MIN_CHUNK_CHARS = 60

SECTION_SEPARATOR = " > "

# ---------------------------------------------------------------------------
# Category rules
#
# The brief asks the knowledge base to cover attractions and neighbourhoods,
# transport, cultural and practical tips, food, itineraries, and indoor/outdoor
# activities. Those map to the tags below. `shopping` and `accommodation` fall
# out of the Wikivoyage section names for free.
# ---------------------------------------------------------------------------
_HEADING_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"\bsee\b|sight|attraction|landmark|museum|galler|architecture and art",
     ("attractions",)),
    (r"\bdo\b|activit|entertainment|nightlife|sport|festival", ("attractions",)),
    (r"\beat\b|\bdrink\b|food|cuisine|restaurant|hawker|cafe|dish|dessert"
     r"|fruit|beverage|snack", ("food",)),
    (r"get in|get around|get out|transport|\bmrt\b|\bbus\b|taxi|train|airport"
     r"|fare|ticketing|network and infrastructure|hours of operation|regulation",
     ("transport",)),
    (r"\bbuy\b|shop|market|mall", ("shopping",)),
    (r"itinerar|day trip|days in|one day|two days|three days|weekend"
     r"|\bday [0-9]|\bafternoon\b|\bmorning\b|\bevening\b", ("itinerary",)),
    (r"understand|culture|cultural|religio|language|etiquette|\bart\b|heritage"
     r"|people|ethnic|national character|\brespect\b|\btalk\b", ("culture",)),
    (r"visa|money|cost|stay safe|stay healthy|health|connect|cope|practical"
     r"|climate|weather|when to|electricity|tipping|emergency|immigration"
     r"|customs|safety|\blearn\b|\bwork\b|\btalk\b|\brespect\b", ("practical",)),
    (r"district|neighbourhood|neighborhood|\bregion|\bareas?\b", ("neighbourhoods",)),
    (r"\bsleep\b|hotel|hostel|accommodation|lodging", ("accommodation",)),
)

HEADING_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), categories)
    for pattern, categories in _HEADING_PATTERNS
)

#: Content keywords. A chunk needs this many distinct matches before it earns a
#: tag, so one passing mention of a park does not make a chunk "outdoor".
MIN_KEYWORD_HITS = 2

INDOOR_KEYWORDS = (
    "museum", "gallery", "aquarium", "mall", "shopping centre", "shopping center",
    "indoor", "air-conditioned", "air conditioning", "theatre", "theater", "cinema",
    "spa", "temple", "mosque", "church", "cathedral", "synagogue", "library",
    "science centre", "planetarium", "food court", "hawker centre", "hawker center",
    "casino", "arcade", "exhibition", "basement", "atrium", "underground",
    "sheltered",
)

OUTDOOR_KEYWORDS = (
    "park", "garden", "beach", "trail", "walking", "cycling", "bicycle", "hike",
    "island", "reservoir", "nature reserve", "boardwalk", "waterfront", "promenade",
    "zoo", "safari", "rooftop", "open-air", "outdoor", "cruise", "quay", "pier",
    "swimming", "kayak", "picnic", "wetland", "mangrove", "esplanade",
)


def _section_path(header_metadata: dict[str, str]) -> str:
    """Build `H1 > H2 > H3` from whichever headers the splitter captured."""
    parts = [header_metadata[key] for key in HEADER_KEYS if key in header_metadata]
    return SECTION_SEPARATOR.join(parts)


def _count_keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    lowered = text.lower()
    return len({keyword for keyword in keywords if keyword in lowered})


def classify(section_path: str, text: str, record: SourceRecord) -> list[str]:
    """Assign categories from the section path, the text and the source."""
    categories: set[str] = set()

    for pattern, tags in HEADING_RULES:
        if pattern.search(section_path):
            categories.update(tags)

    # What kind of document this is tells us things its section names do not:
    # a district guide is about a neighbourhood whatever its headings say, and
    # every chunk of a dedicated itinerary article is itinerary content. Taken
    # from the registry rather than from source-id patterns, so adding a new
    # destination needs no change here.
    if record.kind == "district":
        categories.add("neighbourhoods")
    elif record.kind == "itinerary":
        categories.add("itinerary")

    # Document-level subjects, for chunks whose own heading classifies nothing
    # -- typically a lead section, which carries no H2 at all.
    categories.update(facet for facet in record.facets if facet)

    haystack = f"{section_path}\n{text}"
    if _count_keyword_hits(haystack, INDOOR_KEYWORDS) >= MIN_KEYWORD_HITS:
        categories.add("indoor")
    if _count_keyword_hits(haystack, OUTDOOR_KEYWORDS) >= MIN_KEYWORD_HITS:
        categories.add("outdoor")

    return sorted(categories)


def split_document(record: SourceRecord, body: str) -> list[Document]:
    """Split one document into tagged chunks."""
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=HEADERS_TO_SPLIT_ON, strip_headers=True
    )
    sub_splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=SUB_SPLIT_SEPARATORS,
        keep_separator="start",
    )

    chunks: list[Document] = []
    for section in header_splitter.split_text(body):
        section_path = _section_path(section.metadata) or record.source_title
        pieces = (
            [section.page_content]
            if len(section.page_content) <= settings.chunk_size
            else sub_splitter.split_text(section.page_content)
        )
        for piece in pieces:
            piece = piece.strip()
            if len(piece) < MIN_CHUNK_CHARS:
                continue
            index = len(chunks)
            chunks.append(
                Document(
                    # The section path is prepended so every chunk -- including
                    # sub-chunks that no longer contain their heading -- carries
                    # its own context into the embedding and the citation.
                    page_content=f"{section_path}\n\n{piece}",
                    metadata={
                        "source_id": record.source_id,
                        "source_title": record.source_title,
                        "destination": record.destination,
                        "kind": record.kind,
                        "source_url": record.source_url,
                        "publisher": record.publisher,
                        "license": record.license,
                        "section_path": section_path,
                        "chunk_id": f"{record.source_id}#{index:04d}",
                        "categories": classify(section_path, piece, record),
                    },
                )
            )
    return chunks


def build_chunks(registry: SourceRegistry | None = None) -> list[Document]:
    """Chunk every available source in the registry."""
    registry = registry or SourceRegistry.load()
    registry.reconcile()

    chunks: list[Document] = []
    for record in registry.available():
        path = record.resolved_path
        if path is None:
            continue  # available() guarantees a document, but stay defensive
        _, body = frontmatter.loads(path.read_text(encoding="utf-8"))
        chunks.extend(split_document(record, body))
    return chunks


def chunk_counts_by_source(chunks: list[Document]) -> dict[str, int]:
    """Per-source counts, for the registry and the /admin index status."""
    return dict(Counter(chunk.metadata["source_id"] for chunk in chunks))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def print_stats(chunks: list[Document]) -> None:
    lengths = sorted(len(chunk.page_content) for chunk in chunks)
    by_source = chunk_counts_by_source(chunks)
    by_category: Counter[str] = Counter()
    untagged = 0
    for chunk in chunks:
        categories = chunk.metadata["categories"]
        if categories:
            by_category.update(categories)
        else:
            untagged += 1

    from collections import Counter as _Counter

    by_destination = _Counter(
        chunk.metadata.get("destination") or "(not place-specific)"
        for chunk in chunks
    )
    print(f"{len(chunks):,} chunks from {len(by_source)} sources, "
          f"{len(by_destination)} destination(s)")
    print()
    print("per destination:")
    for name, count in by_destination.most_common():
        print(f"  {count:>5}  {name}")
    print()
    print("length distribution (chars):")
    print(
        f"  min {lengths[0]}  p50 {int(statistics.median(lengths))}  "
        f"mean {int(statistics.fmean(lengths))}  "
        f"p90 {lengths[int(len(lengths) * 0.9)]}  max {lengths[-1]}"
    )
    ceiling = settings.chunk_size + settings.chunk_overlap
    print(f"  over chunk_size+overlap ({ceiling}): "
          f"{sum(1 for n in lengths if n > ceiling)}")
    print()
    print("per source:")
    for source_id, count in sorted(by_source.items(), key=lambda item: -item[1]):
        print(f"  {count:>5}  {source_id}")
    print()
    print("per category:")
    for category, count in by_category.most_common():
        print(f"  {count:>5}  ({100 * count / len(chunks):4.1f}%)  {category}")
    print(f"  {untagged:>5}  ({100 * untagged / len(chunks):4.1f}%)  <no category>")


def print_samples(chunks: list[Document], limit: int) -> None:
    for chunk in chunks[:limit]:
        metadata = chunk.metadata
        print("-" * 78)
        print(f"chunk_id   : {metadata['chunk_id']}")
        print(f"section    : {metadata['section_path']}")
        print(f"categories : {', '.join(metadata['categories']) or '<none>'}")
        print(f"chars      : {len(chunk.page_content)}")
        print()
        print(chunk.page_content[:700])
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chunk the knowledge base.")
    parser.add_argument("--stats", action="store_true", help="Print chunk statistics.")
    parser.add_argument("--sample", type=int, default=0, metavar="N",
                        help="Print N sample chunks.")
    parser.add_argument("--category", metavar="TAG", help="Only chunks with this tag.")
    parser.add_argument("--source", metavar="SOURCE_ID", help="Only this source.")
    parser.add_argument("--destination", metavar="PLACE",
                        help="Only chunks for this destination.")
    parser.add_argument("--untagged", action="store_true",
                        help="Only chunks that earned no category.")
    args = parser.parse_args(argv)

    chunks = build_chunks()
    if not chunks:
        print("No chunks produced. Run `python -m app.ingest.fetch_sources` first.")
        return 1

    filtered = bool(
        args.source or args.category or args.untagged or args.destination
    )
    selected = chunks
    if args.source:
        selected = [c for c in selected if c.metadata["source_id"] == args.source]
    if args.destination:
        from app.ingest.registry import normalise_destination

        wanted = normalise_destination(args.destination)
        selected = [
            c for c in selected
            if normalise_destination(c.metadata.get("destination", "")) == wanted
        ]
    if args.category:
        selected = [c for c in selected if args.category in c.metadata["categories"]]
    if args.untagged:
        selected = [c for c in selected if not c.metadata["categories"]]

    if filtered:
        print(f"{len(selected):,} of {len(chunks):,} chunks match the filter")
        print()
    if not selected:
        return 0

    if args.stats or not (args.sample or filtered):
        print_stats(selected)
    if args.sample:
        print()
        print_samples(selected, args.sample)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
