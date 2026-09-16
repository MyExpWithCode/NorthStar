# Demonstration run sheet

A 5-minute walkthrough showing RAG, MCP, a combined response, conversational context, and honest
degradation. Exact prompts to type and what to point at.

> **Read this first.** Groq's free tier allows **8,000 tokens per minute**. A combined itinerary turn can
> spend most of that on its own, so **pause 30–60 seconds between beats 3 and 6**. If you hit the limit the
> assistant says so plainly — which is itself worth showing (beat 7), but not by accident mid-demo.
> Recording with `LLM_PROVIDER=anthropic` avoids the pauses entirely.

## Before you start

```powershell
.\.venv\Scripts\Activate.ps1
python -m app.ingest.build_index          # only if data/index/ is missing
uvicorn app.api:app --port 8000
```

Open two tabs: `http://localhost:8000/` and `http://localhost:8000/admin`.

Sanity-check in a third tab — `http://localhost:8000/health` should show `"status": "ok"`, a chunk count,
and both MCP servers. **Show this first**: it proves the index and both subprocesses are live before any
claim is made about them.

Have a small `.md` or `.pdf` file ready for beat 2, containing a fact that appears nowhere in a travel
guide. Something like:

```markdown
# Bukit Zarquon Visitor Guide

## Overview
The Bukit Zarquon Observation Deck sits 91 metres above the Kallang basin.
Entry costs 14 dollars for adults and 7 dollars for children under twelve.

## Opening hours
Open daily 9am to 10pm, last entry 9:30pm. Fully sheltered, so it works in wet
weather. A cafe on the upper level serves kaya toast.
```

---

## Beat 1 — What the knowledge base actually is (40s)

**Go to `/admin`.**

Point at:

- **Index header** — built_at, `BAAI/bge-small-en-v1.5`, 384 dimensions, 926 chunks. Say: *nothing here is
  hardcoded; this is what got embedded.*
- **Sources table** — 17 documents, each with its licence and how many chunks it contributes.
- **The three Visit Singapore rows**, marked `unavailable`. Say: *the brief recommended this source. Every
  page on the site is client-rendered — a fetch returns 17 characters of text. Rather than quietly drop it,
  the failed attempt is recorded with the measured reason.*

That last point is the one to land: the system prefers recording a known gap over hiding it.

---

## Beat 2 — Add a document and watch it become citable (60s)

Still in `/admin`.

1. Drop your file on the upload area.
2. **Stop at the preview.** Point at the extracted text, the detected heading count, the editable licence
   field defaulting to `user-supplied`. Say: *nothing is indexed yet. A PDF that extracts as noise is
   visible here, before it can poison the index.*
3. Click **Add to knowledge base**.
4. Point at the **job log** as it moves through `chunking documents` → `embedding N chunks` →
   `writing index` → `swapping index into place` → `retriever reloaded`. Note the chunk count going up.

Say: *the index is built into a temp directory and swapped in only when complete, so chat keeps answering
from the previous index while this runs — and a failed rebuild leaves the working one untouched.*

Return to that document in beat 4.

---

## Beat 3 — RAG only (45s)

**Switch to the chat tab.** Type:

```
What are the must-visit attractions in Singapore?
```

While it answers, point at:

- The **📚 From the knowledge base** label and the `[S1]`-style markers.
- **Expand "Knowledge-base sources"** — clickable Wikivoyage/Wikipedia links, section paths, relevance
  scores, licences. Click one link to show it really goes to the cited page.
- **Expand "Tool calls"** — exactly one call, `search_travel_knowledge_base`. **No weather or currency
  call.** Say: *the brief requires MCP not be used for questions the knowledge base covers, so the prompt
  carries an explicit prohibition, and the test asserts those tools must not fire.*

Worth saying: *these citations come from the retrieval tool's artifact, not from parsing the answer text.
A citation cannot be fabricated — if `[S1]` is there, a real chunk produced it.*

---

## Beat 4 — Your document is now a source (30s)

```
What are the opening hours of the Bukit Zarquon Observation Deck?
```

The answer cites **your uploaded document**, with `user-supplied` as the licence. Say: *this fact appears
in no travel guide. It is answerable only because the document was ingested a minute ago, and it is cited
the same way Wikivoyage is.*

*(Pause ~30 seconds here before the next beat.)*

---

## Beat 5 — MCP only, twice (50s)

```
What is the weather in Singapore right now?
```

Point at **🌐 Live via MCP — get_current_weather (retrieved …)** with the real timestamp, and the tool-call
panel showing `weather` as the server and Open-Meteo as the upstream. **No knowledge-base search** — it
isn't needed.

Then:

```
Convert INR 50,000 to SGD
```

Point at the converted amount, the rate, and **the rate's publication date**. Say: *ECB rates publish once
a business day, so this is often yesterday's rate. Reporting the rate's own date is more honest than
implying a live quote.*

*(Pause ~45 seconds before the next beat — it is the expensive one.)*

---

## Beat 6 — The combined scenario (90s, the centrepiece)

```
Create a three-day Singapore itinerary for next week and adjust it according to the weather forecast.
```

This is the brief's required scenario. When it answers, open the **tool-call panel** and walk through the
order:

1. `get_weather_forecast` — 3 days, with `outdoor_suitability` per day.
2. `search_travel_knowledge_base` — itinerary and attraction content.
3. `search_travel_knowledge_base` **again**, filtered to `indoor`.

Say: *that third call is the point. The agent fetched the forecast, saw that some days were poor for
outdoor activity, and went back to the knowledge base for real indoor alternatives. The weather result
shaped what got retrieved — that is combining both sources, not stapling them together. And the indoor
options are retrieved, not invented: the prompt names the tool and the `categories: ["indoor"]` argument
rather than just asking for "indoor suggestions".*

Then point at the answer itself: day-by-day headings, each block labelled by origin, and the **Sources**
section at the end listing knowledge-base documents and each MCP tool with its upstream service.

---

## Beat 7 — Retained context (40s)

Same conversation, three short turns:

```
I'm travelling with two young children.
```
```
We're on a tight budget too.
```
```
Now build me a two-day plan.
```

The final plan is family-appropriate and budget-conscious **without either constraint being restated**.

Say: *preferences are carried in a LangGraph checkpointer keyed by session. This also exposed a real bug —
retrieval excerpts accumulate in history until the provider rejects the request as too large. Fixed by
clearing older tool results while keeping the most recent, so the current turn always retains its
evidence.*

Click **New conversation** and ask the two-day-plan question again to show the context is genuinely gone.

---

## Beat 8 — Honest failure (45s)

Two quick ones. First, a gap in the knowledge base:

```
What are the best ski resorts in Singapore?
```

The assistant says Singapore has no ski resorts and points to the indoor snow centre it *does* have.

Say: *this one is interesting. That query scores 0.713 for relevance — higher than a legitimate question
about family activities at 0.622. A similarity threshold provably cannot decide this, so the guard has two
layers: a calibrated floor for the clearly-unrelated tail, and the prompt, which receives each excerpt with
its score and is told a high score doesn't mean the passage answers the question.*

Then, an unavailable tool. In a terminal:

```powershell
python scripts/smoke_failures.py
```

Point at the summary: **26 of 26 failure paths behave.** Scroll to the MCP section and read one line aloud
— *"weather service unreachable → ok:false with a reason, no forecast"*.

Say: *the standard here is stricter than "doesn't crash". Every failure must produce something a user can
act on and must not produce a fabricated fact. A made-up forecast is worse than an error message.*

---

## If you have another minute

- **`GET /sources`** — the registry with licences, including the unavailable entries and their reasons.
- **Remove your uploaded document in `/admin`**, then ask the beat-4 question again: the assistant now says
  it doesn't know. Removal deletes the document, its registry entry and its chunks together.
- **`python scripts/smoke_agent.py`** — asserts, per scenario, which tools must and must not fire.

## Closing line

*Three sources of truth — the knowledge base, the MCP tools, and the model's own suggestions — kept
separable in every answer, with citations that come from the retrieval artifact rather than from the
model's prose, and failures that are stated rather than filled in.*
