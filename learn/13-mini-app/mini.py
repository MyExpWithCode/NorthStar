"""NorthStar, minimal. A working travel assistant in one file.

    .venv/Scripts/python.exe learn/13-mini-app/mini.py
    .venv/Scripts/python.exe learn/13-mini-app/mini.py --ask "will it rain in Singapore?"
    .venv/Scripts/python.exe learn/13-mini-app/mini.py --serve

Uses THREE third-party packages - httpx, fastembed, numpy - plus the
standard library. No LangChain, no LangGraph, no FAISS, no MCP, no
FastAPI, no pydantic, no BeautifulSoup.

Imports nothing from app/. Reads app/'s knowledge-base documents
(data/kb/*.md) and builds its own index in learn/_scratch/.

Sections are numbered to match the lessons. Each one says what it
replaced.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
KB_DIR = ROOT / "data" / "kb"
CACHE = ROOT / ".cache" / "fastembed"
SCRATCH = ROOT / "learn" / "_scratch"

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace",
                           line_buffering=True)
    except (AttributeError, OSError, ValueError):
        pass


# =====================================================================
# 1. CONFIG                                       lesson 01
#    replaces: pydantic-settings
# =====================================================================
def load_env() -> dict[str, str]:
    """10 lines instead of a BaseSettings class.

    What we give up: types, bounds, cross-field validation, SecretStr,
    and the shell-shadows-.env detection. At this size, acceptable.
    """
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


ENV = load_env()
GROQ_KEY = ENV.get("GROQ_API_KEY", "").strip()
GROQ_BASE = "https://api.groq.com/openai/v1"
USER_AGENT = "northstar-mini/0.1"       # lesson 07: without this, HTTP 403

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
CHUNK_SIZE = 900
CHUNK_OVERLAP = 120
RELEVANCE_FLOOR = 0.60
RETRIEVAL_K = 5
EXCERPT_CHARS = 520
MAX_ITERATIONS = 8

INDEX_FILE = SCRATCH / "mini-index.npy"
CHUNKS_FILE = SCRATCH / "mini-chunks.json"


# =====================================================================
# 2. KNOWLEDGE BASE                               lessons 03-06
#    replaces: langchain-text-splitters, faiss-cpu, langchain-community
# =====================================================================
HEADING = re.compile(r"^(#{1,3})\s+(.+)$")

INDOOR_WORDS = ("museum", "gallery", "aquarium", "mall", "indoor",
                "air-conditioned", "theatre", "cinema", "temple", "shrine",
                "mosque", "church", "library", "food court",
                "hawker centre", "casino", "exhibition", "sheltered")
OUTDOOR_WORDS = ("park", "garden", "beach", "trail", "walking", "cycling",
                 "hike", "island", "reservoir", "nature reserve",
                 "boardwalk", "waterfront", "promenade", "zoo", "rooftop",
                 "open-air", "outdoor", "quay", "pier")
CATEGORY_RULES = (
    (r"\bsee\b|sight|attraction|museum|galler|landmark", "attractions"),
    (r"\beat\b|\bdrink\b|food|hawker|restaurant|cuisine|cafe", "food"),
    (r"get in|get around|transport|\bmrt\b|\bbus\b|taxi|train|airport|fare",
     "transport"),
    (r"\bbuy\b|shop|market|mall", "shopping"),
    (r"\bsleep\b|hotel|hostel|accommodation|lodging", "accommodation"),
    (r"understand|culture|religio|language|etiquette|heritage", "culture"),
    (r"visa|money|cost|stay safe|health|practical|climate|weather",
     "practical"),
    (r"district|neighbourhood|neighborhood|\bregion|\bareas?\b",
     "neighbourhoods"),
    (r"itinerar|day trip|days in|one day|three days|weekend", "itinerary"),
)
MIN_KEYWORD_HITS = 2
MIN_CHUNK_CHARS = 60


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a `---` metadata block off the top. Replaces PyYAML."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[end + 4:].lstrip()


def split_document(body: str) -> list[tuple[str, str]]:
    """Heading-aware split, then paragraph sub-split.

    Replaces MarkdownHeaderTextSplitter + RecursiveCharacterTextSplitter
    (lesson 03). About 30 lines and genuinely close for markdown.
    """
    # -- stage 1: cut on headings, tracking the h1>h2>h3 path ---------
    sections: list[tuple[str, list[str]]] = []
    path = ["", "", ""]
    current: list[str] = []

    def flush() -> None:
        if current:
            label = " > ".join(p for p in path if p)
            sections.append((label, current[:]))
            current.clear()

    for line in body.splitlines():
        match = HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            path[level - 1] = match.group(2).strip()
            for deeper in range(level, 3):
                path[deeper] = ""
        else:
            current.append(line)
    flush()

    # -- stage 2: sub-split oversized sections on paragraph breaks ----
    out: list[tuple[str, str]] = []
    for label, lines in sections:
        text = "\n".join(lines).strip()
        if not text:
            continue
        if len(text) <= CHUNK_SIZE:
            pieces = [text]
        else:
            pieces, buffer = [], ""
            for paragraph in re.split(r"\n\s*\n", text):
                candidate = (buffer + "\n\n" + paragraph).strip()
                if len(candidate) <= CHUNK_SIZE:
                    buffer = candidate
                    continue
                if buffer:
                    pieces.append(buffer)
                # A single paragraph over the limit: slide a window with
                # overlap, breaking on the last space (lesson 03).
                while len(paragraph) > CHUNK_SIZE:
                    cut = paragraph.rfind(" ", 0, CHUNK_SIZE)
                    cut = cut if cut > CHUNK_SIZE // 2 else CHUNK_SIZE
                    pieces.append(paragraph[:cut])
                    paragraph = paragraph[max(cut - CHUNK_OVERLAP, 0):]
                buffer = paragraph
            if buffer:
                pieces.append(buffer)
        for piece in pieces:
            if len(piece.strip()) >= MIN_CHUNK_CHARS:
                out.append((label, piece.strip()))
    return out


def classify(section_path: str, text: str) -> list[str]:
    """Tag a chunk. Lesson 03, and lesson 04 explains why this is not
    left to the embedding model: 'indoor' and 'outdoor' embed as near
    synonyms (0.78), so the filter has to be hard metadata."""
    tags: set[str] = set()
    for pattern, name in CATEGORY_RULES:
        if re.search(pattern, section_path, re.IGNORECASE):
            tags.add(name)
    low = text.lower()
    if len({w for w in INDOOR_WORDS if w in low}) >= MIN_KEYWORD_HITS:
        tags.add("indoor")
    if len({w for w in OUTDOOR_WORDS if w in low}) >= MIN_KEYWORD_HITS:
        tags.add("outdoor")
    return sorted(tags)


class KnowledgeBase:
    """Chunk store + vector search. Replaces FAISS and its wrapper.

    The "index" is a numpy array and a list of dicts. Lesson 05 measured
    this against real FAISS: identical results, 0.38 ms vs 0.08 ms per
    query, both irrelevant next to a 1-3 second LLM call.
    """

    def __init__(self) -> None:
        self.chunks: list[dict] = []
        self.vectors: np.ndarray | None = None
        self._embedder = None

    # -- embedding -----------------------------------------------------
    @property
    def embedder(self):
        if self._embedder is None:
            from fastembed import TextEmbedding

            self._embedder = TextEmbedding(model_name=EMBEDDING_MODEL,
                                           cache_dir=str(CACHE))
        return self._embedder

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.array(list(self.embedder.embed(texts)), dtype="float32")

    # -- build / load --------------------------------------------------
    def build(self, verbose: bool = True) -> None:
        documents = sorted(KB_DIR.glob("*.md"))
        if not documents:
            raise SystemExit(f"no documents in {KB_DIR}")

        for document in documents:
            meta, body = parse_frontmatter(
                document.read_text(encoding="utf-8")
            )
            for section_path, text in split_document(body):
                self.chunks.append({
                    "text": text,
                    # The section path is PREPENDED before embedding so the
                    # vector contains the destination and section words
                    # (lesson 03). Lesson 06 shows this also inflates every
                    # score for a query naming the destination.
                    "embed_text": f"{section_path}\n\n{text}",
                    "section_path": section_path,
                    "destination": meta.get("destination", ""),
                    "title": meta.get("source_title", document.stem),
                    "url": meta.get("source_url", ""),
                    "license": meta.get("license", "unknown"),
                    "categories": classify(section_path, text),
                    "chunk_id": f"{document.stem}#{len(self.chunks):04d}",
                })

        if verbose:
            print(f"  {len(documents)} documents -> {len(self.chunks)} chunks")
            print(f"  embedding (first run downloads ~130 MB)...")

        started = time.perf_counter()
        self.vectors = self.embed([c["embed_text"] for c in self.chunks])
        if verbose:
            print(f"  embedded in {time.perf_counter() - started:.1f}s "
                  f"-> {self.vectors.shape}")

        SCRATCH.mkdir(parents=True, exist_ok=True)
        np.save(INDEX_FILE, self.vectors)
        CHUNKS_FILE.write_text(
            json.dumps([{k: v for k, v in c.items() if k != "embed_text"}
                        for c in self.chunks]),
            encoding="utf-8",
        )
        if verbose:
            print(f"  saved to {INDEX_FILE.name} + {CHUNKS_FILE.name}")

    def load(self, verbose: bool = True) -> bool:
        if not (INDEX_FILE.is_file() and CHUNKS_FILE.is_file()):
            return False
        self.vectors = np.load(INDEX_FILE)
        self.chunks = json.loads(CHUNKS_FILE.read_text(encoding="utf-8"))
        if len(self.chunks) != len(self.vectors):
            return False
        if verbose:
            print(f"  loaded {len(self.chunks)} chunks from cache")
        return True

    # -- search --------------------------------------------------------
    @property
    def destinations(self) -> list[str]:
        return sorted({c["destination"] for c in self.chunks if c["destination"]})

    def search(self, query: str, destination: str = "",
               categories: list | None = None, k: int = RETRIEVAL_K) -> list[dict]:
        """The entire search. Lessons 05-06.

        Note: filtering happens over ALL scores, not a top-N window, so
        this does NOT have the over-fetch truncation bug lesson 05 found
        in retriever.py. At 1,298 chunks scoring everything is free.
        """
        wanted = {c.strip().lower() for c in (categories or []) if c.strip()}
        place = destination.strip().lower()

        query_vector = self.embed([query])[0]
        scores = self.vectors @ query_vector      # <- the whole search

        candidates = []
        for i in np.argsort(-scores):
            i = int(i)
            score = float(scores[i])
            if score < RELEVANCE_FLOOR:
                break                             # sorted, so we are done
            chunk = self.chunks[i]
            if place and chunk["destination"].lower() != place:
                continue
            # Disjunctive: ANY tag matches. Requiring all of them returns
            # nothing, because few chunks carry four tags (see app/rag).
            if wanted and wanted.isdisjoint(
                {c.lower() for c in chunk["categories"]}
            ):
                continue
            candidates.append({**chunk, "score": round(score, 3)})
            if len(candidates) == k:
                break
        return candidates


# =====================================================================
# 3. TOOLS                                        lessons 08-09
#    replaces: mcp[cli], langchain-mcp-adapters, langchain-core @tool
# =====================================================================
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
    55: "dense drizzle", 61: "slight rain", 63: "rain", 65: "heavy rain",
    80: "rain showers", 81: "rain showers", 82: "violent rain showers",
    95: "thunderstorm", 96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def envelope_ok(data: dict, source: str) -> dict:
    """The ok/source/retrieved_at envelope from lesson 09. Kept, because
    it is a design idea rather than a library feature - and it is what
    lets a failure be DATA the model can report."""
    return {"ok": True, "source": source,
            "retrieved_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "data": data}


def envelope_error(message: str, source: str) -> dict:
    return {"ok": False, "source": source,
            "retrieved_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "error": message}


def outdoor_suitability(rain_mm, rain_pct, code) -> str:
    """Computed by US, not the model (lesson 09). Turns a judgement call
    into a fact the model can act on."""
    code = code or 0
    if (rain_pct or 0) >= 60 or (rain_mm or 0) >= 5 or code in (65, 82, 95, 96, 99):
        return "poor"
    if (rain_pct or 0) >= 30 or (rain_mm or 0) >= 1:
        return "fair"
    return "good"


def geocode(city: str) -> dict | None:
    try:
        response = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1}, timeout=20.0,
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        return results[0] if results else None
    except httpx.HTTPError:
        return None


def get_weather_forecast(city: str = "Singapore", days: int = 3) -> dict:
    source = "Open-Meteo"
    days = max(1, min(int(days or 3), 16))
    location = geocode(city)
    if location is None:
        return envelope_error(f"Could not find a place called {city!r}.", source)
    try:
        response = httpx.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                      "precipitation_sum,precipitation_probability_max"),
            "timezone": location.get("timezone", "auto"),
            "forecast_days": days,
        }, timeout=20.0)
        response.raise_for_status()
        daily = response.json().get("daily") or {}
    except httpx.HTTPError as exc:
        return envelope_error(f"Could not reach the weather service: {exc}",
                              source)

    forecast = []
    for i, date in enumerate(daily.get("time") or []):
        code = (daily.get("weather_code") or [None])[i]
        rain_mm = (daily.get("precipitation_sum") or [None])[i]
        rain_pct = (daily.get("precipitation_probability_max") or [None])[i]
        forecast.append({
            "date": date,
            "temp_max_c": (daily.get("temperature_2m_max") or [None])[i],
            "temp_min_c": (daily.get("temperature_2m_min") or [None])[i],
            "precipitation_mm": rain_mm,
            "precipitation_probability_pct": rain_pct,
            "conditions": WMO.get(code, f"code {code}"),
            "outdoor_suitability": outdoor_suitability(rain_mm, rain_pct, code),
        })
    return envelope_ok({
        "location": f"{location['name']}, {location.get('country', '')}".strip(", "),
        "days_returned": len(forecast),
        "forecast": forecast,
    }, source)


_currencies: dict | None = None


def supported_currencies() -> dict:
    global _currencies
    if _currencies is None:
        try:
            response = httpx.get("https://api.frankfurter.dev/v1/currencies",
                                 timeout=20.0)
            response.raise_for_status()
            _currencies = response.json()
        except httpx.HTTPError:
            _currencies = {}
    return _currencies


def convert_currency(amount: float, from_currency: str,
                     to_currency: str) -> dict:
    source = "Frankfurter (European Central Bank reference rates)"
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return envelope_error(f"`amount` must be a number, got {amount!r}.",
                              source)
    if amount < 0:
        return envelope_error("`amount` cannot be negative.", source)

    source_code = (from_currency or "").strip().upper()
    target_code = (to_currency or "").strip().upper()
    known = supported_currencies()
    # Lesson 09: reject unknown codes, never guess a plausible number.
    for code in (source_code, target_code):
        if known and code not in known:
            return envelope_error(
                f"{code} is not a currency this service publishes rates "
                f"for. It supports {len(known)} currencies including "
                f"{', '.join(sorted(known)[:8])}.", source)
    if source_code == target_code:
        return envelope_ok({"amount": amount, "from_currency": source_code,
                            "to_currency": target_code, "rate": 1.0,
                            "converted_amount": round(amount, 2),
                            "rate_date": "n/a"}, source)
    try:
        response = httpx.get(
            "https://api.frankfurter.dev/v1/latest",
            params={"base": source_code, "symbols": target_code},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        return envelope_error(f"Could not reach the rate service: {exc}",
                              source)

    rate = (payload.get("rates") or {}).get(target_code)
    if rate is None:
        return envelope_error(
            f"No published rate for {source_code}->{target_code}.", source)
    return envelope_ok({
        "amount": amount, "from_currency": source_code,
        "to_currency": target_code, "rate": rate,
        "converted_amount": round(amount * rate, 2),
        # Lesson 09: always surface the rate's own date. ECB publishes
        # once a business day, so a Sunday conversion uses Friday's rate.
        "rate_date": payload.get("date"),
    }, source)


# --- the KB tool, with both sentinels (lesson 06) --------------------
def make_kb_tool(kb: KnowledgeBase, marker_counter: list[int]):
    """`marker_counter` is a one-element list the Agent resets each turn.

    Why it exists: markers must be CONTINUOUS ACROSS A TURN. If each call
    numbers its own excerpts from S1, then a turn with two searches - which
    the flagship scenario always does - emits two [S1]s, two [S2]s, and the
    model's citation is ambiguous.

    The real app/rag/kb_tool.py has this bug: line 225 builds
    `["S" + str(i) for i in range(1, len(hits) + 1)]` per CALL, and
    app/agent.py's extract_provenance dedups by chunk_id without
    renumbering. Found by running this file's flagship scenario and
    noticing two S1-S5 blocks in one provenance list. See lesson 08.
    """

    def search_travel_knowledge_base(
        query: str, destination: str = "", categories: list | None = None,
        k: int = RETRIEVAL_K,
    ) -> dict:
        covered = kb.destinations
        resolved = ""
        if destination.strip():
            match = next(
                (c for c in covered
                 if c.lower() == destination.strip().lower()), None,
            )
            if match is None:
                return {
                    "status": "DESTINATION_NOT_COVERED",
                    "message": (
                        f"The knowledge base has no documents about "
                        f"{destination.strip()!r}. It covers: "
                        f"{', '.join(covered)}. Tell the user plainly and "
                        f"name what is covered. Do not answer from general "
                        f"knowledge. Weather and currency tools still work "
                        f"for any city."
                    ),
                    "covered_destinations": covered,
                    "sources": [],
                }
            resolved = match

        hits = kb.search(query, resolved, categories or [],
                         min(int(k or RETRIEVAL_K), RETRIEVAL_K))
        if not hits:
            return {
                "status": "NO_RELEVANT_CONTENT",
                "message": (
                    f"The knowledge base has nothing"
                    f"{' about ' + resolved if resolved else ''} above the "
                    f"relevance threshold for {query!r}. Tell the user this "
                    f"topic is not covered. Do not answer it from general "
                    f"knowledge."
                ),
                "sources": [],
            }

        excerpts, sources = [], []
        for hit in hits:
            marker_counter[0] += 1
            marker = f"S{marker_counter[0]}"
            body = hit["text"]
            if body.startswith(hit["section_path"]):
                body = body[len(hit["section_path"]):].strip()
            if len(body) > EXCERPT_CHARS:
                body = body[:EXCERPT_CHARS].rsplit(" ", 1)[0] + " ..."
            place = (f" [{hit['destination']}]"
                     if len(kb.destinations) > 1 and hit["destination"] else "")
            excerpts.append(
                f"[{marker}] {hit['section_path']}{place} "
                f"(relevance {hit['score']:.2f})\n{body}"
            )
            # The URL goes in `sources`, NOT in `excerpts` (lesson 08's
            # content_and_artifact split). It would cost tokens on every
            # loop iteration, and the UI already has it.
            sources.append({
                "marker": marker, "title": hit["title"],
                "section_path": hit["section_path"],
                "destination": hit["destination"], "url": hit["url"],
                "license": hit["license"], "chunk_id": hit["chunk_id"],
                "score": hit["score"],
            })
        return {"status": "ok", "excerpts": "\n\n".join(excerpts),
                "sources": sources}

    return search_travel_knowledge_base


# --- hand-written JSON Schemas ---------------------------------------
# This is what langchain-core's @tool generates from type hints. Writing
# it by hand was the single most tedious part of this file, and it can
# silently drift out of sync with the functions above. Lesson 08.
def tool_schemas(covered: list[str]) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "search_travel_knowledge_base",
            "description": (
                "Search the travel knowledge base for destination facts: "
                "attractions, neighbourhoods, transport, food, culture, "
                "practical tips, opening hours, prices and itineraries. The "
                "ONLY permitted source of destination facts - never answer a "
                "destination question from your own knowledge. Not for "
                "weather or exchange rates. If the result status is "
                "DESTINATION_NOT_COVERED or NO_RELEVANT_CONTENT, say so "
                "plainly instead of answering."
            ),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string",
                          "description": "What to search for."},
                "destination": {"type": "string", "description":
                                f"The place in question. Covered: "
                                f"{', '.join(covered)}."},
                "categories": {"type": "array", "items": {"type": "string"},
                               "description":
                               "Optional filter: attractions, "
                               "neighbourhoods, transport, culture, "
                               "practical, food, itinerary, shopping, "
                               "accommodation, indoor, outdoor. [] = all. "
                               "Use ['indoor'] for wet-weather options."},
                "k": {"type": "integer",
                      "description": "Excerpts to return, max 5."},
            }, "required": ["query"]},
        }},
        {"type": "function", "function": {
            "name": "get_weather_forecast",
            "description": (
                "Get the daily weather forecast for any city, up to 16 days "
                "ahead. The ONLY source of weather information - never "
                "estimate a temperature or rain chance. Each day includes "
                "'outdoor_suitability' of good, fair or poor."
            ),
            "parameters": {"type": "object", "properties": {
                "city": {"type": "string", "description": "City name."},
                "days": {"type": "integer",
                         "description": "Days ahead, 1-16."},
            }, "required": ["city"]},
        }},
        {"type": "function", "function": {
            "name": "convert_currency",
            "description": (
                "Convert an amount between currencies at the latest "
                "published ECB reference rate. The ONLY source of exchange "
                "rates - never estimate one. Returns the rate and the date "
                "it was published."
            ),
            "parameters": {"type": "object", "properties": {
                "amount": {"type": "number"},
                "from_currency": {"type": "string",
                                  "description": "ISO 4217, e.g. INR"},
                "to_currency": {"type": "string",
                                "description": "ISO 4217, e.g. SGD"},
            }, "required": ["amount", "from_currency", "to_currency"]},
        }},
    ]


# =====================================================================
# 4. THE LLM                                      lesson 07
#    replaces: langchain-groq, the groq SDK, langchain-core
# =====================================================================
def call_model(messages: list[dict], tools: list[dict],
               max_retries: int = 5) -> dict:
    """One HTTP POST, plus retry-on-429. ~12 lines.

    The User-Agent header is not optional: without it Groq's edge
    returns HTTP 403 with a perfectly valid key (lesson 07). The SDK
    sets one for you, which is a real thing a client library buys.
    """
    if not GROQ_KEY:
        raise SystemExit("GROQ_API_KEY is not set. Add it to .env.")

    payload = {"model": MODEL, "messages": messages, "tools": tools,
               "temperature": 0}
    delay = 2.0
    for attempt in range(max_retries):
        try:
            with httpx.Client(timeout=120.0) as client:
                response = client.post(
                    f"{GROQ_BASE}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {GROQ_KEY}",
                             "User-Agent": USER_AGENT},
                )
            if response.status_code in (429, 500, 502, 503):
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in (429, 500, 502, 503) or attempt == max_retries - 1:
                raise
            wait = float(exc.response.headers.get("Retry-After") or 0) or delay
            print(f"    [HTTP {status}; waiting {wait:.0f}s]", flush=True)
            time.sleep(wait)
            delay = min(delay * 2, 30.0)
    raise RuntimeError("unreachable")


def resolve_model() -> str:
    """Pick a tool-calling model from the LIVE catalogue (lesson 07).

    Groq retires models often enough that a hardcoded id is a time bomb.

    LEARN_MODEL in .env overrides. Groq's daily token limit is PER MODEL
    (200,000 each), so when one is exhausted another has a full budget.
    """
    import os

    override = (os.environ.get("LEARN_MODEL")
                or ENV.get("LEARN_MODEL") or "").strip()
    if override:
        return override

    preferred = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b",
                 "openai/gpt-oss-20b")
    not_chat = ("whisper", "orpheus", "guard", "tts", "embed", "safeguard")
    try:
        request = urllib.request.Request(
            f"{GROQ_BASE}/models",
            headers={"Authorization": f"Bearer {GROQ_KEY}",
                     "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            available = {m["id"] for m in json.load(response).get("data", [])}
    except (urllib.error.URLError, OSError, ValueError):
        return preferred[0]
    for candidate in preferred:
        if candidate in available:
            return candidate
    for candidate in sorted(available):
        low = candidate.lower()
        if not any(x in low for x in not_chat) \
                and not low.startswith("groq/compound"):
            return candidate
    return preferred[0]


MODEL = ""      # set in main()


# =====================================================================
# 5. THE AGENT                                    lesson 10
#    replaces: langchain create_agent, langgraph
# =====================================================================
SYSTEM_PROMPT_TEMPLATE = """You are a travel planning assistant, combining a \
curated travel knowledge base with live information from tools.

DESTINATIONS. The knowledge base covers only: {covered}. Pass the place as the \
`destination` argument when you search it. If a result has status \
DESTINATION_NOT_COVERED, say plainly you have no travel knowledge base for that \
place, name what you do cover, and do NOT describe it from your own knowledge. \
The weather and currency tools work for ANY city, so be explicit about which \
part you can and cannot help with.

SOURCES. Every statement must come from exactly one of three:
1. search_travel_knowledge_base - the ONLY source of destination facts. Never \
answer a destination question from your own knowledge, however confident.
2. get_weather_forecast and convert_currency - the ONLY source of live \
information. Never estimate a temperature, rain chance or exchange rate.
3. Your own reasoning - for organising, sequencing and recommending. Useful, \
but label it a suggestion; never present it as retrieved fact.

TOOL CHOICE.
- Destination question -> knowledge base only.
- Weather or rate question -> that tool only.
- Needs both -> call both and combine.
- When a forecast day has outdoor_suitability "poor", search the knowledge \
base AGAIN with categories ["indoor"] for real indoor alternatives. Never \
invent them.
- Prefer full descriptive queries over short fragments: "where to eat in Kyoto, \
restaurants and local food" retrieves far better than "where to eat".

WHEN YOU LACK INFORMATION.
- status NO_RELEVANT_CONTENT -> say the knowledge base does not cover it. Do \
not fill the gap from memory.
- Excerpts carry a relevance score. A high score does NOT mean the passage \
answers the question. If the text does not contain the answer, say so.
- A tool result with "ok": false -> state which live information could not be \
retrieved and why. Never substitute an estimate.

FORMAT. Cite knowledge-base facts by their [S1] markers. Label live tool \
figures with the source and the date. Be concise.

Today is {today}."""


class Agent:
    """The agent. The loop in `ask` is the whole thing."""

    def __init__(self, kb: KnowledgeBase) -> None:
        self.kb = kb
        # Citation markers run continuously across one turn, then reset.
        # See make_kb_tool's docstring for the bug this avoids.
        self._marker_counter = [0]
        self.registry = {
            "search_travel_knowledge_base": make_kb_tool(
                kb, self._marker_counter
            ),
            "get_weather_forecast": get_weather_forecast,
            "convert_currency": convert_currency,
        }
        self.schemas = tool_schemas(kb.destinations)
        self.system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            covered=", ".join(kb.destinations) or "nothing",
            today=datetime.now(timezone.utc).date().isoformat(),
        )
        # 6. MEMORY (lesson 11): replaces langgraph-checkpoint-sqlite.
        # A dict of message lists. No time travel, no resume.
        self.sessions: dict[str, list[dict]] = {}

    def ask(self, session_id: str, question: str,
            verbose: bool = True) -> tuple[str, dict]:
        messages = self.sessions.setdefault(
            session_id, [{"role": "system", "content": self.system_prompt}]
        )
        messages.append({"role": "user", "content": question})

        self._marker_counter[0] = 0      # markers restart each turn
        sources: list[dict] = []
        tool_calls: list[dict] = []
        seen_chunks: set[str] = set()

        for iteration in range(1, MAX_ITERATIONS + 1):
            try:
                response = call_model(messages, self.schemas)
            except Exception as exc:
                # Lesson 10: a failure must never read as an answer.
                return self._failure(exc), {"kb_sources": [],
                                            "tool_calls": tool_calls}

            message = response["choices"][0]["message"]
            calls = message.get("tool_calls") or []

            if verbose:
                usage = response.get("usage", {})
                print(f"    [iteration {iteration}: "
                      f"{usage.get('prompt_tokens')} prompt tokens, "
                      f"{len(calls)} tool call(s)]", flush=True)

            if not calls:
                answer = message.get("content") or ""
                messages.append({"role": "assistant", "content": answer})
                self._trim(messages)
                return answer, {"kb_sources": sources,
                                "tool_calls": tool_calls}

            messages.append(message)

            for call in calls:
                name = call["function"]["name"]
                try:
                    arguments = json.loads(call["function"]["arguments"] or "{}")
                except ValueError as exc:
                    result = {"ok": False,
                              "error": f"unparseable arguments: {exc}"}
                    arguments = {}
                else:
                    function = self.registry.get(name)
                    if function is None:
                        result = {"ok": False, "error": f"no such tool: {name}"}
                    else:
                        try:
                            result = function(**arguments)
                        except TypeError as exc:
                            # The model omitted or misnamed an argument.
                            # Lesson 08: be LENIENT and say what went wrong,
                            # so it can self-correct on the next iteration.
                            result = {"ok": False,
                                      "error": f"bad arguments: {exc}"}

                if verbose:
                    print(f"      {name}({json.dumps(arguments)[:90]})",
                          flush=True)
                    print(f"        -> {self._summarise(name, result)}",
                          flush=True)

                # Provenance from the RESULT, not from the answer text
                # (lesson 08). A citation cannot be invented.
                for chunk_source in result.get("sources") or []:
                    if chunk_source["chunk_id"] in seen_chunks:
                        continue
                    seen_chunks.add(chunk_source["chunk_id"])
                    sources.append(chunk_source)
                tool_calls.append({
                    "tool": name, "args": arguments,
                    "ok": result.get("ok", result.get("status") == "ok"),
                    "detail": self._summarise(name, result),
                    "source": result.get("source"),
                    "retrieved_at": result.get("retrieved_at"),
                })

                # What the model sees: excerpts WITHOUT urls, or the
                # envelope. Trimmed, because it re-sends every iteration.
                if "excerpts" in result:
                    content = result["excerpts"]
                elif "message" in result:
                    content = f"{result['status']}\n{result['message']}"
                else:
                    content = json.dumps(result)
                messages.append({"role": "tool",
                                 "tool_call_id": call["id"],
                                 "content": content[:6000]})

        return ("I could not complete that within the iteration limit. "
                "No travel information was retrieved."), \
               {"kb_sources": sources, "tool_calls": tool_calls}

    # -- context trimming (lesson 11) ---------------------------------
    def _trim(self, messages: list[dict], trigger: int = 4000,
              keep: int = 3) -> None:
        """ClearToolUsesEdit, by hand. ~10 lines.

        Without this the third turn of a conversation is rejected as too
        large, because retrieval excerpts accumulate and are ~80% of the
        payload.
        """
        size = sum(len(json.dumps(m)) for m in messages) // 4
        if size < trigger:
            return
        indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        for i in indices[:-keep] if len(indices) > keep else []:
            messages[i]["content"] = (
                "[earlier tool result cleared to save context; "
                "search again if you need it]"
            )

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _summarise(name: str, result) -> str:
        if not isinstance(result, dict):
            return str(result)[:110]
        if result.get("status") == "ok":
            return f"{len(result.get('sources') or [])} excerpt(s)"
        if "status" in result:
            return result["status"]
        if result.get("ok") is False:
            return f"FAILED: {str(result.get('error'))[:90]}"
        data = result.get("data") or {}
        if "forecast" in data:
            days = data["forecast"]
            poor = sum(1 for d in days
                       if d.get("outdoor_suitability") == "poor")
            return (f"{len(days)} day(s) for {data.get('location')}, "
                    f"{poor} poor for outdoor activity")
        if "converted_amount" in data:
            return (f"{data.get('amount')} {data.get('from_currency')} = "
                    f"{data.get('converted_amount')} "
                    f"{data.get('to_currency')} "
                    f"(rate published {data.get('rate_date')})")
        return "ok"

    @staticmethod
    def _failure(exc: Exception) -> str:
        text = str(exc)
        if "429" in text or "rate limit" in text.lower():
            return ("The language model is rate-limited right now, so I "
                    "could not complete that request. No travel "
                    "information was retrieved. Please try again shortly.")
        if "413" in text or "too large" in text.lower():
            return ("That request was too large for the provider. No travel "
                    "information was retrieved. Try a shorter question or "
                    "start a new conversation.")
        return (f"I could not complete that request: "
                f"{type(exc).__name__}: {text[:200]}\n\nNothing above is a "
                f"travel fact - please try again in a moment.")


# =====================================================================
# 7. THE SERVER                                   lesson 12
#    replaces: fastapi, uvicorn, python-multipart, pydantic
# =====================================================================
PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>NorthStar mini</title>
<style>
 body{font:15px/1.55 system-ui,sans-serif;max-width:52rem;margin:2rem auto;
      padding:0 1rem;background:#fbfaf8;color:#1a1a1a}
 h1{font-size:1.2rem;margin:0 0 .25rem}
 .sub{color:#666;font-size:.85rem;margin-bottom:1.25rem}
 #log{margin-bottom:1rem}
 .msg{padding:.7rem .9rem;border-radius:.5rem;margin:.5rem 0;
      white-space:pre-wrap}
 .you{background:#e8eef7}
 .bot{background:#fff;border:1px solid #e3e0da}
 .prov{font-size:.78rem;color:#555;border-left:3px solid #c9c4ba;
       padding:.3rem 0 .3rem .7rem;margin:.4rem 0 .9rem}
 .prov a{color:#2a5db0}
 form{display:flex;gap:.5rem}
 input{flex:1;padding:.6rem;border:1px solid #ccc;border-radius:.4rem;
       font-size:15px}
 button{padding:.6rem 1.1rem;border:0;border-radius:.4rem;background:#2a5db0;
        color:#fff;font-size:15px;cursor:pointer}
 button:disabled{background:#999}
</style></head><body>
<h1>NorthStar mini</h1>
<div class="sub">httpx + fastembed + numpy. No LangChain, no FAISS, no MCP,
no FastAPI. Covers: __COVERED__</div>
<div id="log"></div>
<form id="f"><input id="q" placeholder="Ask about your trip..." autofocus>
<button id="b">Send</button></form>
<script>
const log=document.getElementById('log'),f=document.getElementById('f'),
      q=document.getElementById('q'),b=document.getElementById('b');
let session=Math.random().toString(36).slice(2);
function add(cls,text){const d=document.createElement('div');
  d.className='msg '+cls;d.textContent=text;log.appendChild(d);
  window.scrollTo(0,document.body.scrollHeight);return d;}
f.onsubmit=async e=>{e.preventDefault();const text=q.value.trim();
  if(!text)return;q.value='';add('you',text);
  const pending=add('bot','...');b.disabled=true;
  try{
    const r=await fetch('/chat',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:session,question:text})});
    const d=await r.json();
    pending.textContent=d.answer||d.error||'(no answer)';
    const p=d.provenance||{};
    let lines=[];
    (p.tool_calls||[]).forEach(t=>lines.push(
      (t.ok?'\\u2713':'\\u2717')+' '+t.tool+' \\u2014 '+(t.detail||'')));
    const div=document.createElement('div');div.className='prov';
    (p.kb_sources||[]).forEach(s=>{
      const a=document.createElement('a');a.href=s.url;a.target='_blank';
      a.textContent='['+s.marker+'] '+s.section_path+' ('+s.score+')';
      div.appendChild(a);div.appendChild(document.createElement('br'));});
    if(lines.length){const pre=document.createElement('div');
      pre.textContent=lines.join('\\n');div.insertBefore(pre,div.firstChild);}
    if(div.childNodes.length)log.appendChild(div);
  }catch(err){pending.textContent='request failed: '+err;}
  b.disabled=false;q.focus();};
</script></body></html>"""


def serve(agent: Agent, port: int = 8799) -> None:
    """ThreadingHTTPServer instead of FastAPI + uvicorn.

    A thread per request rather than async. Lesson 12 measured what the
    single-threaded version costs; threads fix the blocking but scale
    worse than an event loop for I/O-bound work. No request validation
    beyond the hand-written checks below, and no OpenAPI schema.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    page = PAGE.replace("__COVERED__",
                        ", ".join(agent.kb.destinations) or "nothing")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"    {self.command} {self.path}", flush=True)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode(),
                       "application/json")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/health":
                self._json(200, {
                    "status": "ok", "model": MODEL,
                    "chunks": len(agent.kb.chunks),
                    "destinations": agent.kb.destinations,
                    "tools": list(agent.registry),
                    "sessions": len(agent.sessions),
                    "relevance_floor": RELEVANCE_FLOOR,
                })
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/chat":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            # All of this is what pydantic would do for free.
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                self._json(400, {"error": "body is not valid JSON"})
                return
            if not isinstance(body, dict):
                self._json(400, {"error": "body must be a JSON object"})
                return
            question = body.get("question")
            if not isinstance(question, str) or not question.strip():
                self._json(422, {"error": "question must be a non-empty string"})
                return
            if len(question) > 4000:
                self._json(422, {"error": "question must be under 4000 chars"})
                return
            session_id = str(body.get("session_id") or "web")
            try:
                answer, provenance = agent.ask(session_id, question,
                                               verbose=True)
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            self._json(200, {"answer": answer, "provenance": provenance,
                             "session_id": session_id})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print()
    print(f"  NorthStar mini serving on http://127.0.0.1:{port}")
    print(f"  model: {MODEL}")
    print(f"  Ctrl-C to stop")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
        server.shutdown()


# =====================================================================
# 8. CLI
# =====================================================================
def print_provenance(provenance: dict) -> None:
    for call in provenance.get("tool_calls") or []:
        mark = "OK  " if call.get("ok") else "FAIL"
        print(f"    {mark} {call['tool']:<32} {call.get('detail', '')}")
    for source in provenance.get("kb_sources") or []:
        print(f"    [{source['marker']}] {source['section_path'][:62]} "
              f"({source['score']})")
        if source.get("url"):
            print(f"         {source['url']}")


def main() -> None:
    global MODEL

    parser = argparse.ArgumentParser(description="NorthStar, minimal.")
    parser.add_argument("--ask", metavar="QUESTION",
                        help="ask one question and exit")
    parser.add_argument("--serve", action="store_true",
                        help="start the web UI")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--rebuild", action="store_true",
                        help="re-chunk and re-embed the knowledge base")
    args = parser.parse_args()

    print("NorthStar mini")
    print("=" * 62)
    print("dependencies: httpx, fastembed, numpy + stdlib")
    print()

    kb = KnowledgeBase()
    if args.rebuild or not kb.load():
        print("  building the index...")
        kb.build()
    print(f"  destinations: {', '.join(kb.destinations)}")

    if not GROQ_KEY:
        raise SystemExit(
            "\nGROQ_API_KEY is not set. Add it to .env and re-run.\n"
            "The knowledge base above built fine without it - only the\n"
            "chat needs a key."
        )

    MODEL = resolve_model()
    print(f"  model: {MODEL}")

    agent = Agent(kb)
    print(f"  tools: {', '.join(agent.registry)}")

    if args.serve:
        serve(agent, args.port)
        return

    if args.ask:
        print()
        print(f"> {args.ask}")
        answer, provenance = agent.ask("cli", args.ask)
        print()
        print(answer)
        print()
        print_provenance(provenance)
        return

    # Interactive.
    print()
    print("  Type a question, or 'quit'. Try:")
    print("    what neighbourhoods should I explore in Singapore?")
    print("    will it rain in Singapore in the next 3 days?")
    print("    how much is 50000 INR in SGD?")
    print("    I want outdoor sightseeing in Singapore for 3 days - check")
    print("    the forecast and suggest indoor options for any bad day")
    print()
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if question.lower() in ("quit", "exit", "q"):
            return
        if not question:
            continue
        answer, provenance = agent.ask("cli", question)
        print()
        print(answer)
        print()
        print_provenance(provenance)
        print()


if __name__ == "__main__":
    main()
