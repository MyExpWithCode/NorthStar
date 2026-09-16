"""Lesson 02 - a real web page, through all five stages, with byte counts.

    .venv/Scripts/python.exe learn/02-fetch-html/run.py
    .venv/Scripts/python.exe learn/02-fetch-html/run.py --offline

Imports nothing from app/. This is a from-scratch rebuild of
app/ingest/fetch_sources.py, small enough to read in one go.
"""

import argparse
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = ROOT / "learn" / "_scratch"

URL = "https://en.wikivoyage.org/wiki/Singapore/Bugis"
USER_AGENT = "NorthStar-learning-exercise/0.1 (educational)"

parser = argparse.ArgumentParser()
parser.add_argument("--offline", action="store_true", help="skip the network")
args = parser.parse_args()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def rule(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


# ======================================================================
rule("STAGE 1 - download: 4 lines of stdlib")
# ======================================================================

print("""
    request = urllib.request.Request(url, headers={"User-Agent": ...})
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8")

That is the whole fetcher. No dependency. The User-Agent matters: Wikimedia
returns 403 to a default python-urllib agent.
""")

cached = SCRATCH / "bugis.html"
html = None

if args.offline:
    print("--offline: skipping the network.")
    if cached.is_file():
        html = cached.read_text(encoding="utf-8")
        print(f"using cached {cached.name} ({human(len(html))})")
else:
    print(f"fetching {URL}")
    try:
        request = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8")
        SCRATCH.mkdir(parents=True, exist_ok=True)
        cached.write_text(html, encoding="utf-8")
        print(f"got {human(len(html))} of HTML  (cached to learn/_scratch/)")
    except (urllib.error.URLError, OSError) as exc:
        print(f"network failed: {exc}")
        if cached.is_file():
            html = cached.read_text(encoding="utf-8")
            print(f"falling back to cached {cached.name}")

if html is None:
    print()
    print("No HTML available. Skipping to the local-document section.")
else:
    raw_len = len(html)

    # ==================================================================
    rule("STAGE 2 - parse: BeautifulSoup builds a tree")
    # ==================================================================

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    print("BeautifulSoup does NOT clean anything. It gives you a tree.")
    print()
    tags = {}
    for element in soup.find_all(True):
        tags[element.name] = tags.get(element.name, 0) + 1
    print("the 12 most common tags on this page:")
    for name, count in sorted(tags.items(), key=lambda kv: -kv[1])[:12]:
        print(f"    {count:>5}  <{name}>")
    print()
    print(f"total elements: {sum(tags.values())}")

    # ==================================================================
    rule("STAGE 3 - clean: YOU decide what to delete")
    # ==================================================================

    # A trimmed version of _REMOVE_SELECTORS from the real fetcher (32 there).
    REMOVE = (
        "script", "style", "noscript", "link", "meta",
        "sup.reference", ".mw-editsection", ".navbox", ".sidebar",
        ".infobox", "table.infobox", ".metadata", ".hatnote",
        "#toc", ".toc", ".noprint", ".printfooter",
        "figure", "figcaption", ".thumb", ".gallery",
        ".mw-kartographer-map", ".mw-kartographer-maplink",
        "nav", "header", "footer", "aside",
    )

    print("deleting by CSS selector, counting as we go:")
    print()
    removed_total = 0
    for selector in REMOVE:
        found = soup.select(selector)
        if not found:
            continue
        for element in found:
            element.decompose()      # remove AND destroy. .extract() would
            removed_total += 1       # hand the node back instead.
        print(f"    {len(found):>4} x  {selector}")
    print()
    print(f"removed {removed_total} elements")

    # Section-level dropping: by MEANING, not by structure.
    DROP_SECTIONS = (
        "references", "see also", "external links",
        "further reading", "notes", "gallery", "go next",
    )
    print()
    print("now dropping whole sections by their HEADING TEXT -")
    print("these are real prose with no class name to catch them:")
    print()
    dropped_sections = 0
    for heading in soup.find_all(["h2", "h3"]):
        title = heading.get_text(" ", strip=True).lower()
        title = re.sub(r"\[.*?\]", "", title).strip()
        if title not in DROP_SECTIONS:
            continue
        print(f"    dropping section: {title!r}")
        # delete the heading and every sibling until the next heading
        for sibling in list(heading.next_siblings):
            if getattr(sibling, "name", None) in ("h2", "h3"):
                break
            if hasattr(sibling, "decompose"):
                sibling.decompose()
        heading.decompose()
        dropped_sections += 1
    if not dropped_sections:
        print("    (none found on this page)")

    # The article body only. Everything outside it is site chrome.
    body = soup.select_one("#mw-content-text") or soup.body or soup
    cleaned_html = str(body)

    print()
    print(f"HTML after cleaning + narrowing to #mw-content-text: "
          f"{human(len(cleaned_html))}")

    # ==================================================================
    rule("STAGE 4 - convert: markdownify, and why heading_style matters")
    # ==================================================================

    from markdownify import markdownify

    atx = markdownify(cleaned_html, heading_style="ATX", strip=["a", "img"])
    under = markdownify(cleaned_html, heading_style="UNDERLINED", strip=["a", "img"])

    atx = re.sub(r"\n{3,}", "\n\n", atx).strip()
    under = re.sub(r"\n{3,}", "\n\n", under).strip()

    print('heading_style="ATX"  (what NorthStar uses):')
    for line in atx.splitlines():
        if line.startswith("#"):
            print(f"    {line}")

    print()
    print('heading_style="UNDERLINED"  (aka setext, the alternative):')
    lines = under.splitlines()
    shown = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i and stripped and set(stripped) in ({"="}, {"-"}):
            print(f"    {lines[i - 1]}")
            print(f"    {line}")
            shown += 1
        elif stripped.startswith("###"):
            print(f"    {line}        <- still ATX!")
            shown += 1
        if shown >= 8:
            break

    atx_headings = len(re.findall(r"^#+ ", atx, re.M))
    under_headings = len(re.findall(r"^#+ ", under, re.M))
    print()
    print(f"  headings a '#'-based splitter can find:")
    print(f"    ATX        : {atx_headings}")
    print(f"    UNDERLINED : {under_headings}   <- only the h3s survive")
    print()
    print("  ^ ATX is NOT a style preference. Lesson 03's")
    print("    MarkdownHeaderTextSplitter looks for '#' characters, so with")
    print("    UNDERLINED it loses every h1 and h2 and the whole chunking")
    print("    strategy silently degrades toward blind character splitting.")
    print("    (Note setext can only express TWO levels, so h3 falls back")
    print("    to ### regardless - which is its own argument against it.)")
    print()
    print("    The option is set in parsers.py; the reason lives in")
    print("    chunk.py. That coupling is very easy to miss.")

    # ---- a real gotcha, found while writing this lesson ----------------
    typo = markdownify(cleaned_html, heading_style="SETEXT", strip=["a", "img"])
    typo_headings = len(re.findall(r"^#+ ", re.sub(r"\n{3,}", "\n\n", typo), re.M))
    print()
    print("  GOTCHA. markdownify's valid values are ATX, ATX_CLOSED and")
    print("  UNDERLINED. 'SETEXT' is the standard name for the underlined")
    print("  style, so it is the obvious thing to type - and markdownify")
    print("  accepts it without complaint and quietly does ATX instead:")
    print(f"    heading_style='SETEXT' -> {typo_headings} '#' headings "
          f"(identical to ATX's {atx_headings})")
    print()
    print("  An unrecognised option that raises nothing and changes nothing")
    print("  is the worst kind. Check library output, not library docs.")

    # ==================================================================
    rule("STAGE 5 - frontmatter: the metadata a citation needs")
    # ==================================================================

    frontmatter = f"""---
source_id: wikivoyage-singapore-bugis
source_title: "Wikivoyage: Singapore/Bugis"
source_url: "{URL}"
publisher: Wikivoyage
license: CC BY-SA 4.0
destination: Singapore
retrieved_at: "{datetime.now(timezone.utc).isoformat(timespec='seconds')}"
---
"""
    print(frontmatter)
    print("Written now because now is the only time you know it.")
    print("'retrieved_at' is unrecoverable after the fact, and without")
    print("publisher/license/url the UI cannot render [S1] as a link.")

    document = frontmatter + "\n" + atx + "\n"
    out = SCRATCH / "bugis.md"
    SCRATCH.mkdir(parents=True, exist_ok=True)
    out.write_text(document, encoding="utf-8")

    # ==================================================================
    rule("THE NUMBERS")
    # ==================================================================

    print(f"  1. raw HTML                        {human(raw_len):>10}   100%")
    print(f"  2. cleaned HTML                    {human(len(cleaned_html)):>10}"
          f"   {len(cleaned_html) / raw_len:>5.0%}")
    print(f"  3. markdown                        {human(len(atx)):>10}"
          f"   {len(atx) / raw_len:>5.0%}")
    print(f"  4. + frontmatter -> _scratch/bugis.md")
    print()
    print(f"  headings preserved : {len(re.findall(r'^#+ ', atx, re.M))}")
    print(f"  words              : {len(atx.split())}")
    print()
    print("We threw away ~90% of the bytes. That is the job. What is left")
    print("is what a traveller would actually read - and crucially it still")
    print("has its heading structure, which lesson 03 needs.")

    print()
    print("first 500 chars of the markdown body:")
    print("-" * 68)
    print(atx[:500])
    print("-" * 68)


# ======================================================================
rule("THE FINISHED ARTICLE - a document already in data/kb/")
# ======================================================================

existing = ROOT / "data" / "kb" / "wikivoyage-singapore-bugis.md"
if not existing.is_file():
    candidates = sorted((ROOT / "data" / "kb").glob("*.md"))
    existing = candidates[0] if candidates else None

if existing and existing.is_file():
    text = existing.read_text(encoding="utf-8")
    print(f"{existing.name}  ({human(len(text))})")
    print()
    print("its frontmatter, as committed:")
    print("-" * 68)
    inside = False
    for line in text.splitlines():
        if line.strip() == "---":
            print(line)
            if inside:
                break
            inside = True
            continue
        if inside:
            print(line)
    print("-" * 68)
    print()
    headings = re.findall(r"^(#+)\s+(.+)$", text, flags=re.MULTILINE)
    print(f"{len(headings)} headings. The first 15:")
    for hashes, title in headings[:15]:
        print(f"    {'  ' * (len(hashes) - 1)}{hashes} {title}")
    print()
    print("Those headings are the cut points for lesson 03.")
else:
    print("no documents in data/kb/ yet")


# ======================================================================
rule("THE PDF PROBLEM")
# ======================================================================
print("""
  A PDF has no <h2>. It has text at a larger font size, which is not the
  same thing. pypdf's extract_text() returns a flat run of characters.

  Consequence: an uploaded PDF produces chunks with NO section path, so
    - lesson 03's heading splitter has nothing to split on and falls
      back to blind character chunks
    - the citation shown in the UI has no section to name
    - retrieval quality is measurably worse

  That is not a bug you can fix downstream. It is why app/ingest/parsers.py
  carries this docstring line:

      Every parser has the same obligation: preserve the heading
      hierarchy.

  and why it REFUSES a document under MIN_EXTRACTED_CHARS (200) rather
  than indexing it as filler. A scanned PDF with no text layer yields
  almost nothing, and an empty document silently degrades every later
  answer.
""")

print()
print("Next: learn/03-chunking/README.md")
