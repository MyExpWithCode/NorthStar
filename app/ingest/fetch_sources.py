"""Seed the curated knowledge base.

Fetches the curated travel sources, converts them to heading-preserving
markdown, writes them to `data/kb/` with provenance frontmatter, and records
every one in the source registry.

    python -m app.ingest.fetch_sources
    python -m app.ingest.fetch_sources --only wikivoyage-singapore --only wikipedia-singapore-mrt
    python -m app.ingest.fetch_sources --skip-existing
    python -m app.ingest.fetch_sources --list

Heading hierarchy matters: the chunker splits on markdown headings so that a
retrieved chunk stays self-contained and carries a readable section path such
as `Singapore > Get around > MRT`. Anything that flattens headings here
degrades retrieval later.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode

from bs4 import BeautifulSoup
from markdownify import markdownify

from app.config import settings
from app.ingest import frontmatter
from app.ingest.registry import (
    SourceRecord,
    SourceRegistry,
    relative_doc_path,
    utc_now_iso,
)

# Wikimedia asks clients to identify themselves. No personal contact details
# are sent to third-party services.
USER_AGENT = "NorthStar-TravelAssistant/0.1 (educational assignment project)"
REQUEST_TIMEOUT = 30.0
_REQUEST_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
#: Minimum interval between outbound requests. Wikimedia rate-limits a plain
#: sequential crawl at well under one request per second and answers 429.
POLITE_DELAY_SECONDS = 1.5
#: Transient statuses worth retrying rather than recording as unavailable.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 5.0
#: Monotonic timestamp of the last request, for the throttle.
_last_request_at = 0.0
#: Below this much extracted body text we treat a page as not server-rendered
#: rather than writing a near-empty document into the knowledge base.
MIN_USABLE_TEXT_CHARS = 2_000

CC_BY_SA = "CC BY-SA 4.0"

#: The destination shipped with the project. Others are added with
#: `--add-destination`, which needs no code change.
SINGAPORE = "Singapore"

#: Reference apparatus that is never useful for travel planning.
ALWAYS_DROP_SECTIONS = (
    "references",
    "see also",
    "external links",
    "further reading",
    "notes",
    "bibliography",
    "citations",
    "gallery",
    "go next",
)

#: Chrome, navigation and map widgets. Wikivoyage POI listings are deliberately
#: NOT in this list -- they carry addresses, opening hours and prices.
_REMOVE_SELECTORS = (
    "script",
    "style",
    "noscript",
    "link",
    "meta",
    "sup.reference",
    "sup.mw-ref",
    ".mw-editsection",
    ".navbox",
    ".vertical-navbox",
    ".sidebar",
    ".infobox",
    "table.infobox",
    ".metadata",
    ".ambox",
    ".hatnote",
    ".shortdescription",
    ".reflist",
    ".refbegin",
    "#toc",
    ".toc",
    ".noprint",
    ".sistersitebox",
    ".printfooter",
    "figure",
    "figcaption",
    ".thumb",
    ".gallery",
    ".mw-kartographer-map",
    ".mw-kartographer-maplink",
    ".listing-coordinates",
    ".geo",
    ".mw-empty-elt",
    ".mw-file-element",
    ".mw-jump-link",
)


@dataclass(frozen=True)
class CuratedSource:
    source_id: str
    source_title: str
    publisher: str
    license: str
    #: Wikimedia host, e.g. `en.wikivoyage.org`. None means a generic web page.
    wiki_host: str | None = None
    #: Wikimedia page title.
    page_title: str | None = None
    #: Direct URL, for generic pages.
    page_url: str | None = None
    #: The place this document is about. "" for non-place-specific documents.
    destination: str = ""
    #: guide | district | itinerary | reference -- see registry.Kind.
    kind: str = "reference"
    #: Subjects of the whole document, used to tag chunks whose own heading
    #: classifies nothing (a lead section has no H2).
    facets: tuple[str, ...] = field(default=())
    #: Commit the document itself to git. Only for permissive licences.
    committed: bool = True
    #: Top-level sections to drop, lower-cased. Used to keep 200 kB reference
    #: articles focused on what a traveller actually needs.
    drop_sections: tuple[str, ...] = field(default=())

    @property
    def url(self) -> str:
        if self.page_url:
            return self.page_url
        return f"https://{self.wiki_host}/wiki/{quote(self.page_title or '', safe='/')}"

    @property
    def rest_html_url(self) -> str:
        return (
            f"https://{self.wiki_host}/api/rest_v1/page/html/"
            f"{quote(self.page_title or '', safe='')}"
        )

    @property
    def document_path(self) -> Path:
        return settings.kb_dir / f"{self.source_id}.md"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _wikivoyage_guide(destination: str, page_title: str | None = None) -> CuratedSource:
    """A destination's main Wikivoyage travel guide."""
    page_title = page_title or destination
    return CuratedSource(
        source_id=f"wikivoyage-{_slug(destination)}",
        source_title=f"Wikivoyage: {page_title} Travel Guide",
        publisher="Wikivoyage",
        license=CC_BY_SA,
        wiki_host="en.wikivoyage.org",
        page_title=page_title,
        destination=destination,
        kind="guide",
    )


def _wikivoyage_district(
    destination: str, page_title: str, slug: str | None = None
) -> CuratedSource:
    """A district or neighbourhood guide within a destination."""
    slug = slug or _slug(page_title.split("/")[-1])
    return CuratedSource(
        source_id=f"wikivoyage-{_slug(destination)}-{slug}",
        source_title=f"Wikivoyage: {page_title}",
        publisher="Wikivoyage",
        license=CC_BY_SA,
        wiki_host="en.wikivoyage.org",
        page_title=page_title,
        destination=destination,
        kind="district",
    )


def _wikivoyage_itinerary(
    destination: str, slug: str, page_title: str
) -> CuratedSource:
    """A standalone Wikivoyage itinerary article.

    The "Itineraries" section of the main Singapore guide is only a list of
    links to these articles, so without fetching them the knowledge base has
    almost no day-by-day planning content -- which is exactly what the brief's
    flagship scenario asks for.
    """
    return CuratedSource(
        source_id=f"wikivoyage-itinerary-{slug}",
        source_title=f"Wikivoyage: {page_title}",
        publisher="Wikivoyage",
        license=CC_BY_SA,
        wiki_host="en.wikivoyage.org",
        page_title=page_title,
        destination=destination,
        kind="itinerary",
    )


# ---------------------------------------------------------------------------
# The curated source set.
#
# The brief asks for at least three public resources. This is 15 documents.
#
# Visit Singapore is listed last and is expected to FAIL: every page on that
# site is client-side rendered, so a plain HTTP fetch returns roughly 100
# characters of text. We keep the attempt in the registry, marked unavailable
# with the reason, rather than silently pretending we never tried. Its facets
# (practical info, itineraries, things to do) are covered by the Wikivoyage and
# Wikipedia sources below. See docs/ARCHITECTURE.md section 3.
# ---------------------------------------------------------------------------
CURATED_SOURCES: tuple[CuratedSource, ...] = (
    # -- Wikivoyage: the travel guide proper -------------------------------
    _wikivoyage_guide(SINGAPORE),
    _wikivoyage_district(SINGAPORE, "Singapore/Marina Bay", "marina-bay"),
    _wikivoyage_district(SINGAPORE, "Singapore/Riverside", "riverside"),
    _wikivoyage_district(SINGAPORE, "Singapore/Orchard", "orchard"),
    _wikivoyage_district(SINGAPORE, "Singapore/Chinatown", "chinatown"),
    _wikivoyage_district(SINGAPORE, "Singapore/Little India", "little-india"),
    _wikivoyage_district(SINGAPORE, "Singapore/Bugis", "bugis"),
    _wikivoyage_district(SINGAPORE, "Singapore/Sentosa and Harbourfront", "sentosa-harbourfront"),
    _wikivoyage_district(SINGAPORE, "Singapore/East Coast", "east-coast"),
    _wikivoyage_district(SINGAPORE, "Singapore/North and West", "north-and-west"),
    _wikivoyage_district(SINGAPORE, "Singapore/Balestier", "balestier"),
    # -- Wikivoyage: day-by-day itineraries --------------------------------
    _wikivoyage_itinerary(SINGAPORE, "three-days", "Three days in Singapore"),
    _wikivoyage_itinerary(SINGAPORE, "southern-ridges-walk", "Southern Ridges Walk"),
    # -- Wikipedia: transport, food, attractions, culture ------------------
    CuratedSource(
        source_id="wikipedia-singapore-mrt",
        destination=SINGAPORE,
        kind="reference",
        facets=("transport",),
        source_title="Wikipedia: Mass Rapid Transit (Singapore)",
        publisher="Wikipedia",
        license=CC_BY_SA,
        wiki_host="en.wikipedia.org",
        page_title="Mass Rapid Transit (Singapore)",
        # Keep network, fares and passenger regulations; drop the railfan and
        # corporate history that would otherwise dominate transport retrieval.
        drop_sections=(
            "history",
            "rolling stock and signalling",
            "future expansion",
            "performance",
            "ridership",
            "security",
        ),
    ),
    CuratedSource(
        source_id="wikipedia-singaporean-cuisine",
        destination=SINGAPORE,
        kind="reference",
        facets=("food",),
        source_title="Wikipedia: Singaporean cuisine",
        publisher="Wikipedia",
        license=CC_BY_SA,
        wiki_host="en.wikipedia.org",
        page_title="Singaporean cuisine",
        drop_sections=(
            "history",
            "singapore food internationally",
            "singaporean dishes uncommon in singapore",
        ),
    ),
    CuratedSource(
        source_id="wikipedia-tourism-in-singapore",
        destination=SINGAPORE,
        kind="reference",
        facets=("attractions",),
        source_title="Wikipedia: Tourism in Singapore",
        publisher="Wikipedia",
        license=CC_BY_SA,
        wiki_host="en.wikipedia.org",
        page_title="Tourism in Singapore",
        drop_sections=("history", "tourism statistics"),
    ),
    CuratedSource(
        source_id="wikipedia-culture-of-singapore",
        destination=SINGAPORE,
        kind="reference",
        facets=("culture",),
        source_title="Wikipedia: Culture of Singapore",
        publisher="Wikipedia",
        license=CC_BY_SA,
        wiki_host="en.wikipedia.org",
        page_title="Culture of Singapore",
        drop_sections=("history", "cultural policy", "popular culture"),
    ),
    # -- Visit Singapore: attempted, expected to be unavailable ------------
    CuratedSource(
        source_id="visitsingapore-essential",
        destination=SINGAPORE,
        kind="reference",
        source_title="Visit Singapore: Essential Travel Information",
        publisher="Visit Singapore (Singapore Tourism Board)",
        license="All rights reserved - not redistributed",
        page_url="https://www.visitsingapore.com/travel-guide-tips/essential-travel-information/",
        committed=False,
    ),
    CuratedSource(
        source_id="visitsingapore-itineraries",
        destination=SINGAPORE,
        kind="reference",
        source_title="Visit Singapore: Sample Itineraries",
        publisher="Visit Singapore (Singapore Tourism Board)",
        license="All rights reserved - not redistributed",
        page_url="https://www.visitsingapore.com/see-do-singapore/itineraries/",
        committed=False,
    ),
    CuratedSource(
        source_id="visitsingapore-things-to-do",
        destination=SINGAPORE,
        kind="reference",
        source_title="Visit Singapore: Things to Do",
        publisher="Visit Singapore (Singapore Tourism Board)",
        license="All rights reserved - not redistributed",
        page_url="https://www.visitsingapore.com/see-do-singapore/",
        committed=False,
    ),
)


#: Wikivoyage subpages that are not travel content for the destination.
_SKIP_SUBPAGE_WORDS = ("archive", "talk", "sandbox", "template")


def discover_wikivoyage_destination(destination: str) -> list[CuratedSource]:
    """Find the Wikivoyage guide and district pages for a destination.

    Wikivoyage names district guides as subpages -- `Singapore/Chinatown`,
    `Tokyo/Shinjuku` -- so the whole set for a city can be discovered from the
    MediaWiki API rather than hand-listed. Redirects are skipped, since several
    of them point at one district under different names.

    Raises LookupError when the destination has no Wikivoyage guide, so a typo
    produces a clear message instead of an empty knowledge base.
    """
    host = "en.wikivoyage.org"

    def api(params: dict[str, str]) -> dict:
        query = urlencode({**params, "format": "json", "formatversion": "2"})
        return json.loads(_http_get(f"https://{host}/w/api.php?{query}"))

    info = api({"action": "query", "prop": "info", "titles": destination})
    pages = info.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        raise LookupError(
            f"Wikivoyage has no article titled {destination!r}. Check the "
            "spelling, or use the exact title from en.wikivoyage.org."
        )
    if "redirect" in pages[0]:
        raise LookupError(
            f"{destination!r} is a redirect on Wikivoyage. Use the title it "
            "redirects to."
        )

    sources = [_wikivoyage_guide(destination, pages[0]["title"])]

    listing = api({
        "action": "query",
        "list": "allpages",
        "apprefix": destination + "/",
        "apnamespace": "0",
        "aplimit": "100",
    })
    subpages = [p["title"] for p in listing.get("query", {}).get("allpages", [])]
    if subpages:
        details = api({
            "action": "query",
            "prop": "info",
            "titles": "|".join(subpages[:50]),
        })
        for page in details.get("query", {}).get("pages", []):
            title = page.get("title", "")
            if page.get("missing") or "redirect" in page:
                continue
            leaf = title.split("/")[-1].lower()
            if any(word in leaf for word in _SKIP_SUBPAGE_WORDS):
                continue
            sources.append(_wikivoyage_district(destination, title))
    return sources


class SourceUnavailable(RuntimeError):
    """Raised when a source cannot be turned into a usable document."""


# ---------------------------------------------------------------------------
# HTML -> markdown
# ---------------------------------------------------------------------------
def _heading_of(section) -> str | None:
    heading = section.find(["h1", "h2", "h3", "h4", "h5", "h6"], recursive=False)
    if heading is None:
        # Wikimedia sometimes wraps the heading one level deeper.
        heading = section.find(["h2", "h3"])
    if heading is None:
        return None
    return heading.get_text(" ", strip=True).lower()


def _drop_sections(soup: BeautifulSoup, names: set[str]) -> list[str]:
    """Remove whole `<section>` blocks whose heading matches `names`."""
    dropped: list[str] = []
    for section in soup.find_all("section"):
        if section.decomposed:
            continue
        heading = _heading_of(section)
        if heading and heading in names:
            dropped.append(heading)
            section.decompose()

    # Fallback for markup without <section> wrappers: remove every sibling
    # between a matching heading and the next heading of the same level.
    for level in ("h2", "h3"):
        for heading in soup.find_all(level):
            if heading.get_text(" ", strip=True).lower() not in names:
                continue
            dropped.append(heading.get_text(" ", strip=True).lower())
            for sibling in list(heading.find_next_siblings()):
                if sibling.name == level:
                    break
                sibling.decompose()
            heading.decompose()
    return dropped


def _strip_chrome(soup: BeautifulSoup) -> None:
    for selector in _REMOVE_SELECTORS:
        for element in soup.select(selector):
            element.decompose()


_MARKDOWN_CLEANUPS = (
    # markdownify leaves the bracketed edit affordances behind on some pages.
    (re.compile(r"\[\s*edit\s*\]", re.IGNORECASE), ""),
    # Empty headings left by removed widgets.
    (re.compile(r"^#{1,6}\s*$", re.MULTILINE), ""),
    # Reference leftovers like "[1]" / "[citation needed]".
    (re.compile(r"\[\s*(?:\d+|citation needed)\s*\]", re.IGNORECASE), ""),
    # Trailing whitespace hurts nothing but makes diffs noisy.
    (re.compile(r"[ \t]+$", re.MULTILINE), ""),
    # Collapse the blank-line runs that decomposing elements leaves behind.
    (re.compile(r"\n{3,}"), "\n\n"),
)


def html_to_markdown(html: str, drop_sections: tuple[str, ...] = ()) -> str:
    """Convert article HTML to clean markdown with its heading hierarchy intact."""
    soup = BeautifulSoup(html, "html.parser")
    _strip_chrome(soup)
    _drop_sections(soup, set(ALWAYS_DROP_SECTIONS) | {s.lower() for s in drop_sections})

    markdown = markdownify(
        str(soup),
        heading_style="ATX",
        # Render links and images as their text; inline URLs add noise to the
        # embeddings without adding retrievable meaning.
        strip=["a", "img", "sup"],
        # Backslash-escaping punctuation would show up verbatim in citations.
        escape_asterisks=False,
        escape_underscores=False,
        escape_misc=False,
    )
    for pattern, replacement in _MARKDOWN_CLEANUPS:
        markdown = pattern.sub(replacement, markdown)
    return markdown.strip() + "\n"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _throttle() -> None:
    """Keep outbound requests at most one per POLITE_DELAY_SECONDS."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < POLITE_DELAY_SECONDS:
        time.sleep(POLITE_DELAY_SECONDS - elapsed)
    _last_request_at = time.monotonic()


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """Honour a `Retry-After` header when the server sends one."""
    raw = exc.headers.get("Retry-After") if exc.headers else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


def _http_get(url: str) -> str:
    """Fetch a URL as text using the standard library.

    Deliberately not the httpx client. Wikimedia's bot protection answers that
    library with 403 regardless of User-Agent, Accept, Accept-Encoding,
    Connection or ALPN settings -- it fingerprints the client below the HTTP
    layer -- while urllib is served normally. The MCP servers in T6/T7 still
    use httpx, because their upstreams do no such filtering. Do not
    "modernise" this transport without re-testing against Wikimedia first.
    """
    request = urllib.request.Request(url, headers=_REQUEST_HEADERS)
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_STATUS or attempt == MAX_RETRIES:
                raise
            delay = _retry_after_seconds(exc) or RETRY_BACKOFF_SECONDS * attempt
            print(
                f"     HTTP {exc.code}; retrying in {delay:.0f}s "
                f"(attempt {attempt}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise AssertionError("unreachable: retry loop exhausted without raising")


def fetch_wikimedia(source: CuratedSource) -> str:
    """Fetch a Wikimedia page as markdown via the REST HTML endpoint."""
    html = _http_get(source.rest_html_url)
    markdown = html_to_markdown(html, source.drop_sections)
    if len(markdown) < MIN_USABLE_TEXT_CHARS:
        raise SourceUnavailable(
            f"only {len(markdown)} characters extracted; page may be a redirect"
        )
    return markdown


def fetch_generic(source: CuratedSource) -> str:
    """Fetch an ordinary web page as markdown.

    Pages rendered entirely on the client yield almost no text here. We raise
    rather than write a near-empty document, so the registry records an honest
    `unavailable` instead of the index gaining a source that says nothing.
    """
    html = _http_get(source.url)
    soup = BeautifulSoup(html, "html.parser")
    _strip_chrome(soup)
    container = soup.find("main") or soup.find("article") or soup.body or soup
    markdown = html_to_markdown(str(container))
    if len(markdown) < MIN_USABLE_TEXT_CHARS:
        raise SourceUnavailable(
            f"only {len(markdown)} characters of server-rendered text "
            "(page appears to be client-side rendered)"
        )
    return markdown


def write_document(source: CuratedSource, markdown: str, retrieved_at: str) -> Path:
    """Write the document with provenance frontmatter and an H1 title."""
    metadata = {
        "source_id": source.source_id,
        "source_title": source.source_title,
        "source_url": source.url,
        "publisher": source.publisher,
        "license": source.license,
        "origin": "curated",
        "destination": source.destination,
        "kind": source.kind,
        "facets": ",".join(source.facets),
        "retrieved_at": retrieved_at,
    }
    body = f"# {source.source_title}\n\n{markdown}"
    path = source.document_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(metadata, body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
@dataclass
class FetchOutcome:
    source: CuratedSource
    state: str
    detail: str
    chars: int = 0


def fetch_all(
    sources: tuple[CuratedSource, ...] = CURATED_SOURCES,
    *,
    skip_existing: bool = False,
) -> list[FetchOutcome]:
    registry = SourceRegistry.load()
    outcomes: list[FetchOutcome] = []

    for source in sources:
        retrieved_at = utc_now_iso()

        if skip_existing and source.document_path.is_file():
            outcomes.append(FetchOutcome(source, "skipped", "already on disk"))
            continue

        try:
            if source.wiki_host:
                markdown = fetch_wikimedia(source)
            else:
                markdown = fetch_generic(source)
        except (SourceUnavailable, urllib.error.URLError, OSError) as exc:
            reason = str(exc) or exc.__class__.__name__
            registry.upsert(
                SourceRecord(
                    source_id=source.source_id,
                    source_title=source.source_title,
                    source_url=source.url,
                    publisher=source.publisher,
                    license=source.license,
                    origin="curated",
                    destination=source.destination,
                    kind=source.kind,
                    facets=list(source.facets),
                    doc_path=None,
                    committed=source.committed,
                    state="unavailable",
                    note=reason,
                )
            )
            outcomes.append(FetchOutcome(source, "unavailable", reason))
            continue

        path = write_document(source, markdown, retrieved_at)
        registry.upsert(
            SourceRecord(
                source_id=source.source_id,
                source_title=source.source_title,
                source_url=source.url,
                publisher=source.publisher,
                license=source.license,
                origin="curated",
                destination=source.destination,
                kind=source.kind,
                facets=list(source.facets),
                doc_path=relative_doc_path(path),
                retrieved_at=retrieved_at,
                committed=source.committed,
                state="available",
            )
        )
        outcomes.append(FetchOutcome(source, "ok", path.name, chars=len(markdown)))

    registry.reconcile()
    registry.save()
    return outcomes


def print_status_table(outcomes: list[FetchOutcome]) -> None:
    symbols = {"ok": "OK", "skipped": "--", "unavailable": "!!"}
    id_width = max((len(o.source.source_id) for o in outcomes), default=10)

    print()
    dest_width = max(
        (len(o.source.destination or "-") for o in outcomes), default=6
    )
    print(f"{'':2}  {'source_id'.ljust(id_width)}  "
          f"{'destination'.ljust(dest_width)}  {'chars':>7}  detail")
    print(f"{'-' * 2}  {'-' * id_width}  {'-' * dest_width}  "
          f"{'-' * 7}  {'-' * 40}")
    for outcome in outcomes:
        chars = f"{outcome.chars:,}" if outcome.chars else ""
        detail = outcome.detail
        if len(detail) > 60:
            detail = detail[:57] + "..."
        print(
            f"{symbols[outcome.state]:2}  "
            f"{outcome.source.source_id.ljust(id_width)}  "
            f"{(outcome.source.destination or '-').ljust(dest_width)}  "
            f"{chars:>7}  {detail}"
        )

    ok = [o for o in outcomes if o.state == "ok"]
    unavailable = [o for o in outcomes if o.state == "unavailable"]
    skipped = [o for o in outcomes if o.state == "skipped"]
    publishers = {o.source.publisher for o in ok}

    print()
    print(
        f"{len(ok)} fetched, {len(skipped)} skipped, {len(unavailable)} unavailable "
        f"| {len(publishers)} publisher(s) | {sum(o.chars for o in ok):,} chars"
    )
    if unavailable:
        print(
            "Unavailable sources are recorded in the registry with their reason; "
            "they are not silently dropped."
        )
    print(f"Registry: {settings.registry_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the destination knowledge base. Seeds the curated "
                    "set by default; --add-destination adds another place."
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="SOURCE_ID",
        help="Fetch only this source id. Repeatable.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Leave documents that are already on disk untouched.",
    )
    parser.add_argument(
        "--list", action="store_true", help="List the curated source ids and exit."
    )
    parser.add_argument(
        "--add-destination",
        metavar="PLACE",
        help="Discover and fetch a new destination's Wikivoyage guide and "
             "district pages, e.g. --add-destination Tokyo. Rebuild the index "
             "afterwards with `python -m app.ingest.build_index`.",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="With --add-destination, list the pages that would be fetched "
             "and exit without fetching them.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for source in CURATED_SOURCES:
            print(f"{source.source_id:40}  {source.publisher:16}  {source.url}")
        return 0

    if args.add_destination:
        try:
            discovered = discover_wikivoyage_destination(args.add_destination)
        except (LookupError, urllib.error.URLError, OSError) as exc:
            parser.error(str(exc))
        print(f"{args.add_destination}: {len(discovered)} page(s) on Wikivoyage")
        for source in discovered:
            print(f"  {source.kind:9} {source.page_title}")
        if args.discover_only:
            return 0
        print()
        outcomes = fetch_all(tuple(discovered), skip_existing=args.skip_existing)
        print_status_table(outcomes)
        if any(o.state == "ok" for o in outcomes):
            print()
            print("Now rebuild the index so these become searchable:")
            print("  python -m app.ingest.build_index")
        return 0

    selected = CURATED_SOURCES
    if args.only:
        wanted = set(args.only)
        selected = tuple(s for s in CURATED_SOURCES if s.source_id in wanted)
        unknown = wanted - {s.source_id for s in selected}
        if unknown:
            parser.error(
                f"unknown source id(s): {', '.join(sorted(unknown))}. "
                "Use --list to see the available ids."
            )

    outcomes = fetch_all(selected, skip_existing=args.skip_existing)
    print_status_table(outcomes)

    # A failed optional source is a warning, not a build failure -- but no
    # usable sources at all means the knowledge base would be empty.
    if not any(o.state in {"ok", "skipped"} for o in outcomes):
        print("\nERROR: no sources could be fetched.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
