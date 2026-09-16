# 02 — Fetching HTML and turning it into clean markdown

**Real files:** [app/ingest/fetch_sources.py](../../app/ingest/fetch_sources.py),
[app/ingest/parsers.py](../../app/ingest/parsers.py)
**Libraries:** `urllib` (stdlib), `beautifulsoup4`, `markdownify`, `pypdf`, `python-docx`

This is step [1] and [2] of Flow A. It has nothing to do with AI. It is the
unglamorous half of RAG and it decides the quality of everything downstream —
**if the text you index is full of navigation menus, your search results will
be full of navigation menus.**

## The problem, concretely

A Wikivoyage page is roughly 400 KB of HTML. About 60 KB of it is the article.
The rest is:

- `<script>` and `<style>`
- the nav sidebar, repeated on every page
- "Edit this page", "Talk", breadcrumbs
- infoboxes, coordinate widgets, map embeds
- the footer, the licence notice, the language list

You want the 60 KB. You want it as **markdown**, not plain text, because
markdown keeps the `##` headings — and lesson 03 cuts the document at headings.
Strip to plain text and you have thrown away the structure you are about to
need.

## Three sub-problems, three tools

```
  urllib          bytes over the network
     |
     v
  BeautifulSoup   parse HTML into a tree, DELETE the junk nodes
     |
     v
  markdownify     serialise the surviving tree as markdown
```

They are separate libraries because they are separate jobs. Keep them separate
in your head.

### 1. Downloading — why stdlib `urllib` and not `httpx`

`requirements.txt` says this explicitly, and it is worth understanding:

> The fetcher uses stdlib urllib, not httpx. httpx arrives in T6 for the MCP servers.

The fetcher runs once, offline, sequentially, from a command line. It needs:
a URL, a User-Agent header, and bytes back. `urllib.request` does that in four
lines with zero dependencies.

`httpx` earns its place in the **MCP servers** (lesson 09), where the
requirements are different: async, connection reuse, timeouts, and a live
request on the user's critical path.

That is the general rule and it is worth internalising: *a dependency should be
justified by the requirement at the site where it is used, not by being the
nicer library in the abstract.*

### 2. Cleaning — BeautifulSoup

BeautifulSoup does not clean anything by itself. It gives you a tree and a
query language; **you** decide what to delete. `app/ingest/parsers.py` does:

```python
soup = BeautifulSoup(html, "html.parser")
for element in soup.select("script, style, nav, footer, .mw-editsection, ..."):
    element.decompose()          # remove the node and its children
```

`decompose()` is the important method. Two things people get wrong:

- `.extract()` removes the node and hands it back; `.decompose()` removes and
  destroys it. Use `decompose` unless you want the node.
- Deleting by CSS selector is site-specific. `.mw-editsection` is a MediaWiki
  class. There is no universal junk list, which is why generic extractors
  (below) exist.

The real fetcher has **32 selectors** in `_REMOVE_SELECTORS`, and a comment on
that list worth reading:

> Wikivoyage POI listings are deliberately NOT in this list — they carry
> addresses, opening hours and prices.

That is the whole judgement call of this stage in one line. Cleaning is lossy,
and over-cleaning quietly deletes the facts you are building the app to serve.

There is a second, coarser filter too — `ALWAYS_DROP_SECTIONS` removes whole
sections *by heading text*: "References", "See also", "External links",
"Further reading", "Gallery". Those sections are real prose that BeautifulSoup
has no class name to catch, and they are pure noise for travel planning. Note
the two different mechanisms: **selectors delete by structure, section names
delete by meaning.**

### 3. Converting — markdownify

`markdownify` walks the cleaned tree and emits markdown. One option matters:

```python
markdownify(html, heading_style="ATX")
```

ATX means `## Heading`. The alternative — `heading_style="UNDERLINED"`, usually
called setext — writes `Heading` followed by a line of `---`. Lesson 03's
`MarkdownHeaderTextSplitter` looks for `#` characters, so ATX is not a style
preference — **it is a requirement for the next stage to work at all.** With
`UNDERLINED` this page drops from 13 findable headings to 5.

This is the kind of coupling that makes a pipeline confusing to read. The
option is set in `parsers.py`; the reason lives in `chunk.py`.

**A gotcha `run.py` demonstrates:** markdownify accepts only `ATX`,
`ATX_CLOSED` and `UNDERLINED`. "Setext" is the standard name for the underlined
style, so `heading_style="SETEXT"` is the natural thing to type — and
markdownify accepts it silently and does ATX anyway. An unrecognised option
that raises nothing and changes nothing is the worst kind to debug.

## Frontmatter: the metadata that survives

Each file in `data/kb/` starts with a YAML-ish block:

```markdown
---
source_url: https://en.wikivoyage.org/wiki/Singapore
source_title: "Wikivoyage: Singapore"
destination: Singapore
publisher: Wikivoyage
license: CC BY-SA 4.0
fetched_at: 2026-09-16T10:02:11+00:00
---

# Singapore
...
```

This exists so a citation can be rendered later. When the UI shows `[S1]` as a
clickable link with a licence, this block is where that came from. It is
written at fetch time because that is the only moment you know it —
`fetched_at` in particular is unrecoverable afterwards.

`app/ingest/frontmatter.py` is 78 lines and parses this by hand rather than
using `PyYAML`. Reasonable: the block is generated by the same codebase that
reads it, so the full YAML spec is not needed.

## Uploads: PDF and DOCX

The `/admin` UI accepts file uploads, so two more parsers appear:

| Format | Library | What you get |
|---|---|---|
| `.pdf` | `pypdf` | `page.extract_text()` per page. **Headings are lost** — a PDF has no `<h2>`, only text at a larger font size. |
| `.docx` | `python-docx` | paragraphs with style names, so `Heading 1` *can* be recovered |
| `.md`, `.txt` | none | already text |
| `.html` | BeautifulSoup | same path as a URL |

The PDF caveat matters. A PDF ingests as one long flat run of text, so lesson
03's heading splitter has nothing to split on and falls back to character
chunks. Retrieval quality from PDFs is measurably worse for this reason, and
it is not a bug you can fix downstream.

## Alternatives

### Downloading
| Tool | Sync/async | Notes |
|---|---|---|
| `urllib.request` | sync | stdlib, zero deps. **Used here.** Clumsy API, no timeouts by default. |
| `requests` | sync | the friendly classic. Not async. |
| `httpx` | both | requests-like API + async + HTTP/2. **Used by the MCP servers.** |
| `aiohttp` | async | older async standard, heavier API |
| `scrapy` | async framework | when you are crawling thousands of pages with queues, retries and politeness rules |

### HTML cleaning + extraction
| Tool | Approach | When |
|---|---|---|
| `beautifulsoup4` + `markdownify` | **you** specify what to delete | **Used here.** Full control, needs per-site selectors |
| `trafilatura` | heuristic main-content extraction | excellent default for arbitrary news/article URLs; less control |
| `readability-lxml` | the Firefox Reader algorithm | same idea, older |
| `lxml` directly | fast C parser + XPath | when BeautifulSoup is the bottleneck (rarely) |
| `selectolax` | very fast CSS | large-scale scraping |
| `html2text` | HTML -> markdown | alternative to markdownify; different escaping choices |
| `docling`, `unstructured` | multi-format document -> structured | heavyweight; good PDF layout/table handling |
| Firecrawl / Jina Reader (APIs) | hosted URL -> markdown | zero code, per-page cost, external dependency |
| Playwright / Selenium | real browser | **the only option for JavaScript-rendered pages** |

**When would you switch?** Two clear triggers:

1. **Ingesting arbitrary user-supplied URLs.** Per-site CSS selectors do not
   scale past a handful of known sites. Reach for `trafilatura`.
2. **The page is a JS app.** `urllib` gets you an empty `<div id="root">`.
   Nothing but a real browser will help.

NorthStar ingests a fixed, known set of Wikivoyage/Wikipedia pages, so
hand-written selectors are the right amount of machinery.

### PDF
| Tool | Notes |
|---|---|
| `pypdf` | **Used here.** Pure python, no system deps, basic text only |
| `pdfplumber` | better layout + table extraction |
| `PyMuPDF` (fitz) | fastest, best quality; AGPL licence — check before shipping |
| `unstructured`, `docling` | structure-aware, can recover headings |
| AWS Textract / Azure DI | OCR for scanned documents, per-page cost |

## Run it

```bash
.venv/Scripts/python.exe learn/02-fetch-html/run.py
```

It fetches one real Wikivoyage page, then shows the same page at five stages:
raw HTML, parsed, junk removed, converted to markdown, with frontmatter. With
byte counts at each step, so you can see the 400 KB become 60 KB. It also
inspects a document already in `data/kb/` to show the finished shape.

Add `--offline` to skip the network and use only the local files.

## Next

[03 — Chunking](../03-chunking/) — where to cut the markdown, and why.
