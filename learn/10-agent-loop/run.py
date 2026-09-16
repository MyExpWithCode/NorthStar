"""Lesson 10 - a working agent in 40 lines, then the framework version.

    .venv/Scripts/python.exe learn/10-agent-loop/run.py

Needs GROQ_API_KEY. Uses the REAL knowledge base index and the REAL MCP
servers, but the loop itself is hand-written. Imports nothing from app/.
"""

import json
import pickle
import subprocess
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from _shared import GROQ_BASE, groq_key, post_json, resolve_model, rule

ROOT = Path(__file__).resolve().parents[2]
INDEX_DIR = ROOT / "data" / "index"
CACHE = ROOT / ".cache" / "fastembed"

KEY = groq_key()
MODEL = resolve_model(KEY)
URL = f"{GROQ_BASE}/chat/completions"

FLOOR = 0.60
MAX_ITERATIONS = 8

print(f"model: {MODEL}")


# ======================================================================
# The real knowledge base (lessons 03-06, condensed)
# ======================================================================
print("loading the index and embedding model...")

import faiss
from fastembed import TextEmbedding

embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5",
                         cache_dir=str(CACHE))
index = faiss.read_index(str(INDEX_DIR / "index.faiss"))
docstore, mapping = pickle.loads((INDEX_DIR / "index.pkl").read_bytes())
DOCS = [docstore._dict[mapping[i]] for i in range(index.ntotal)]
VECTORS = np.vstack([index.reconstruct(i) for i in range(index.ntotal)])
COVERED = sorted({d.metadata.get("destination", "") for d in DOCS} - {""})
print(f"ready: {len(DOCS)} chunks covering {', '.join(COVERED)}")


def search_travel_knowledge_base(
    query: str, destination: str = "", categories: list | None = None,
    k: int = 5,
) -> dict:
    """Lessons 03-06 in one function, including both sentinels."""
    resolved = None
    if destination.strip():
        resolved = next(
            (c for c in COVERED if c.lower() == destination.strip().lower()),
            None,
        )
        if resolved is None:
            return {
                "status": "DESTINATION_NOT_COVERED",
                "message": (
                    f"No documents about {destination!r}. "
                    f"Covers: {', '.join(COVERED)}. Say so plainly; do not "
                    "answer from general knowledge."
                ),
                "sources": [],
            }

    wanted = {c.strip().lower() for c in (categories or []) if c.strip()}
    q = np.array(list(embedder.embed([query])))[0]
    scores = VECTORS @ q
    # Over-fetch then filter, exactly as retriever.py does (lesson 05).
    order = np.argsort(-scores)[:k * 6 if (wanted or resolved) else k]

    hits, sources = [], []
    for i in order:
        i = int(i)
        score = float(scores[i])
        if score < FLOOR:
            continue
        meta = DOCS[i].metadata
        if resolved and meta.get("destination") != resolved:
            continue
        if wanted and wanted.isdisjoint(
            {c.lower() for c in meta.get("categories") or []}
        ):
            continue
        marker = f"S{len(hits) + 1}"
        section = meta.get("section_path", "")
        body = DOCS[i].page_content.strip()
        if body.startswith(section):
            body = body[len(section):].strip()
        hits.append(f"[{marker}] {section} (relevance {score:.2f})\n"
                    f"{body[:520]}")
        sources.append({"marker": marker, "section": section,
                        "url": meta.get("source_url"),
                        "destination": meta.get("destination"),
                        "score": round(score, 3)})
        if len(hits) == k:
            break

    if not hits:
        return {
            "status": "NO_RELEVANT_CONTENT",
            "message": (
                f"Nothing about {resolved or 'anywhere'} above the relevance "
                f"threshold for {query!r}. Tell the user this topic is not "
                "covered. Do not answer from general knowledge."
            ),
            "sources": [],
        }
    return {"status": "ok", "excerpts": "\n\n".join(hits), "sources": sources}


# ======================================================================
# The real MCP servers (lesson 09, condensed)
# ======================================================================
class McpServer:
    """A minimal stdio MCP client. Lesson 09 without the printing."""

    def __init__(self, module: str) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", module],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=str(ROOT),
            text=True, encoding="utf-8", bufsize=1,
        )
        self._id = 0
        self._rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "learn-10", "version": "0.1"},
        })
        self._notify("notifications/initialized")
        self.tools = (self._rpc("tools/list").get("result") or {}).get(
            "tools", []
        )

    def _write(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._write({"jsonrpc": "2.0", "id": self._id, "method": method,
                     **({"params": params} if params is not None else {})})
        return json.loads(self.process.stdout.readline())

    def _notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def call(self, name: str, arguments: dict) -> dict:
        result = self._rpc("tools/call",
                           {"name": name, "arguments": arguments})
        content = (result.get("result") or {}).get("structuredContent")
        if content is not None:
            return content
        for block in (result.get("result") or {}).get("content") or []:
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except (TypeError, ValueError):
                    return {"ok": False, "error": block["text"][:300]}
        return {"ok": False, "error": "unreadable MCP result"}

    def close(self) -> None:
        try:
            self.process.stdin.close()
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()


print("starting MCP servers...")
SERVERS: dict[str, McpServer] = {}
DEGRADED: dict[str, str] = {}
for name, module in (("weather", "app.mcp_servers.weather_server"),
                     ("currency", "app.mcp_servers.currency_server")):
    try:
        SERVERS[name] = McpServer(module)
    except Exception as exc:
        DEGRADED[name] = f"{type(exc).__name__}: {exc}"

# --- assemble the tool list the model will see ------------------------
TOOL_SCHEMAS = [{
    "type": "function",
    "function": {
        "name": "search_travel_knowledge_base",
        "description": (
            "Search the travel knowledge base for destination facts: "
            "attractions, neighbourhoods, transport, food, culture, practical "
            "tips, opening hours, prices, itineraries. The ONLY permitted "
            "source of destination facts - never answer a destination "
            "question from your own knowledge. Not for weather or rates."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "destination": {"type": "string", "description":
                                f"One of: {', '.join(COVERED)}"},
                "categories": {
                    "type": "array", "items": {"type": "string"},
                    "description": (
                        "Optional filter: attractions, neighbourhoods, "
                        "transport, culture, practical, food, itinerary, "
                        "shopping, accommodation, indoor, outdoor. [] = all."
                    ),
                },
                "k": {"type": "integer", "description": "Excerpts, max 5"},
            },
            "required": ["query"],
        },
    },
}]

REGISTRY = {"search_travel_knowledge_base": search_travel_knowledge_base}

TOOL_SERVER: dict[str, str] = {}

for server_name, server in SERVERS.items():
    for tool in server.tools:
        TOOL_SCHEMAS.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                # The MCP inputSchema IS a JSON Schema already (lesson 09),
                # so it goes straight through with no translation.
                "parameters": tool.get("inputSchema", {}),
            },
        })
        TOOL_SERVER[tool["name"]] = server_name


def run_tool(name: str, arguments: dict):
    """Dispatch: local function, or MCP server, or an honest failure."""
    if name in REGISTRY:
        return REGISTRY[name](**arguments)
    server_name = TOOL_SERVER.get(name)
    if server_name:
        return SERVERS[server_name].call(name, arguments)
    return {"ok": False, "error": f"no such tool: {name}"}


SYSTEM_PROMPT = f"""You are a travel planning assistant.

DESTINATIONS. The knowledge base covers only: {', '.join(COVERED)}.
Pass the place as the `destination` argument. If a result has status
DESTINATION_NOT_COVERED, say plainly you have no guide for that place and
name what you do cover. Do NOT describe it from your own knowledge.

SOURCES. Every statement must come from exactly one of:
1. search_travel_knowledge_base -- the ONLY source of destination facts.
2. The weather and currency tools -- the ONLY source of live information.
   Never estimate a temperature, rain chance or exchange rate.
3. Your own reasoning -- for sequencing and recommending. Label it as a
   suggestion; never present it as retrieved fact.

TOOL CHOICE. When a forecast day is poor for outdoor activity, search the
knowledge base AGAIN with categories ["indoor"] for real indoor
alternatives. Never invent them.

WHEN YOU LACK INFORMATION. status NO_RELEVANT_CONTENT -> say the knowledge
base does not cover it. A tool result with "ok": false -> say which live
information could not be retrieved and why. Never substitute an estimate.

Cite knowledge-base excerpts by their [S1] markers. Be concise."""

if DEGRADED:
    SYSTEM_PROMPT += (
        "\n\nTOOL AVAILABILITY: unavailable right now: "
        + ", ".join(DEGRADED)
        + ". If asked for it, say plainly it cannot be retrieved. "
        "Never estimate or recall it."
    )

print(f"tools available: {len(TOOL_SCHEMAS)}")
for schema in TOOL_SCHEMAS:
    name = schema["function"]["name"]
    origin = TOOL_SERVER.get(name, "local")
    print(f"  {name:<34} ({origin})")
if DEGRADED:
    print(f"degraded: {DEGRADED}")


# ======================================================================
# THE AGENT. This is the whole thing.
# ======================================================================
def agent(question: str, history: list | None = None,
          verbose: bool = True) -> tuple[str, list, list]:
    """The agent loop. Everything below the docstring is ~25 lines.

    Returns (answer, provenance, messages).
    """
    messages = history or [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append({"role": "user", "content": question})
    provenance: list[dict] = []

    for iteration in range(1, MAX_ITERATIONS + 1):
        response = post_json(URL, {
            "model": MODEL,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "temperature": 0,
        }, KEY)

        choice = response["choices"][0]
        message = choice["message"]
        usage = response.get("usage", {})

        if verbose:
            print(f"\n  --- iteration {iteration} "
                  f"(prompt_tokens {usage.get('prompt_tokens')}, "
                  f"finish {choice['finish_reason']!r}) ---")

        calls = message.get("tool_calls") or []
        if not calls:
            if verbose:
                print("  no tool calls -> the model is done")
            return message.get("content") or "", provenance, messages

        messages.append(message)

        for call in calls:
            name = call["function"]["name"]
            try:
                arguments = json.loads(call["function"]["arguments"] or "{}")
            except ValueError as exc:
                # A model generated this string. It can be malformed.
                result = {"ok": False,
                          "error": f"unparseable arguments: {exc}"}
                arguments = {}
            else:
                result = run_tool(name, arguments)

            if verbose:
                print(f"    tool  {name}")
                print(f"    args  {json.dumps(arguments)[:150]}")
                print(f"    ->    {summarise(name, result)}")

            provenance.append({"iteration": iteration, "tool": name,
                               "args": arguments, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result)[:6000],
            })

    return ("I could not complete that within the iteration limit. "
            "No travel information was retrieved."), provenance, messages


def summarise(name: str, result) -> str:
    """One line per tool result, for the trace."""
    if not isinstance(result, dict):
        return str(result)[:110]
    if "status" in result:
        if result["status"] == "ok":
            return f"{len(result.get('sources') or [])} excerpt(s)"
        return result["status"]
    if result.get("ok") is False:
        return f"ok=False: {str(result.get('error'))[:90]}"
    data = result.get("data") or {}
    if "forecast" in data:
        days = data["forecast"]
        poor = sum(1 for d in days if d.get("outdoor_suitability") == "poor")
        return (f"{len(days)} day(s) for {data.get('location')}, "
                f"{poor} poor for outdoor activity")
    if "converted_amount" in data:
        return (f"{data.get('amount')} {data.get('from_currency')} = "
                f"{data.get('converted_amount')} {data.get('to_currency')} "
                f"(rate published {data.get('rate_date')})")
    return "ok"


# ======================================================================
rule("THE LOOP, IN FULL - this is the entire agent")
# ======================================================================
print("""
    messages = [system, user]

    for iteration in range(MAX_ITERATIONS):
        response = POST /chat/completions {messages, tools}
        message  = response.choices[0].message

        if not message.tool_calls:
            return message.content              # done

        messages.append(message)
        for call in message.tool_calls:
            result = run_tool(call.name, json.loads(call.arguments))
            messages.append({"role": "tool",
                             "tool_call_id": call.id,
                             "content": json.dumps(result)})

  That is it. ~15 lines of control flow. Everything create_agent does
  is this plus production concerns.
""")


# ======================================================================
rule("RUN 1 - a single knowledge-base question")
# ======================================================================
print('question: "What are the main neighbourhoods to explore in Singapore?"')

answer, prov, _ = agent(
    "What are the main neighbourhoods to explore in Singapore?"
)
print()
print(f"  {len(prov)} tool call(s), then prose:")
print()
for line in answer.splitlines()[:14]:
    print(f"      {line}")


# ======================================================================
rule("RUN 2 - THE FLAGSHIP SCENARIO")
# ======================================================================
print("""
  "I am in Singapore for the next 3 days and wanted to do outdoor
   sightseeing. Check the forecast, and if any day is bad for being
   outside, suggest indoor alternatives instead."

  Watch for the SECOND knowledge-base search with categories=['indoor'].
  Nothing in the code decides to do that. The model decides, AFTER it
  sees that a forecast day came back 'poor'.
""")

answer2, prov2, messages2 = agent(
    "I am in Singapore for the next 3 days and wanted to do outdoor "
    "sightseeing. Check the forecast, and if any day is bad for being "
    "outside, suggest indoor alternatives instead."
)

print()
print("  the trace:")
print()
print(f"    {'#':<3} {'iter':<5} {'tool':<34} {'arguments'}")
print(f"    {'-' * 3} {'-' * 5} {'-' * 34} {'-' * 30}")
for i, entry in enumerate(prov2, 1):
    args = json.dumps(entry["args"])
    print(f"    {i:<3} {entry['iteration']:<5} {entry['tool']:<34} "
          f"{args[:44]}")

indoor_searches = [
    p for p in prov2
    if p["tool"] == "search_travel_knowledge_base"
    and "indoor" in [c.lower() for c in (p["args"].get("categories") or [])]
]
weather_calls = [p for p in prov2 if "weather" in p["tool"]]

print()
if weather_calls and indoor_searches:
    print("  ** IT WORKED. **")
    print()
    print(f"  The model called the weather tool, saw a 'poor' day, and THEN")
    print(f"  searched the knowledge base with categories=['indoor'].")
    print()
    print("  ** That second search is conditional on data that did not")
    print("     exist when the question was asked. ** No keyword router")
    print("     can express that. It is the actual reason agents exist:")
    print("     conditional multi-step work where later steps depend on")
    print("     earlier RESULTS.")
elif weather_calls:
    print("  The model checked the weather but did not do an indoor-filtered")
    print("  search this run. That happens - it is a non-deterministic")
    print("  system, which is why app/prompts.py states the rule")
    print("  explicitly rather than hoping. Re-run to see it vary.")
else:
    print("  The model did not call the weather tool this run.")

print()
print("  the answer:")
print()
for line in answer2.splitlines()[:30]:
    print(f"      {line}")


# ======================================================================
rule("THE THREE THINGS THIS NAIVE LOOP GETS WRONG")
# ======================================================================

print("""
  1. NO ITERATION CAP (well - we added one, MAX_ITERATIONS=8).
     Without it, a model that keeps calling tools loops forever and
     every iteration costs money. Always bound it.

  2. SEQUENTIAL TOOL EXECUTION.
     Look at the trace above. If two tool calls arrived in the same
     iteration, we ran them one after the other. They are independent
     and should run concurrently. create_agent does this.

  3. CONTEXT GROWS WITHOUT LIMIT.
     Every tool result stays in `messages` forever, and every
     iteration re-sends all of it.
""")

total_chars = sum(len(json.dumps(m)) for m in messages2)
tool_chars = sum(
    len(json.dumps(m)) for m in messages2 if m.get("role") == "tool"
)
print(f"  after ONE turn of the flagship question:")
print(f"    messages in history      {len(messages2)}")
print(f"    total size               {total_chars:,} chars "
      f"(~{total_chars // 4:,} tokens)")
print(f"    of which tool results    {tool_chars:,} chars "
      f"({tool_chars / total_chars:.0%})")
print()
print("  Tool results are the bulk of it, and they accumulate. Groq's")
print("  free tier allows a few thousand tokens per minute. app/agent.py")
print("  hit HTTP 413 on turn THREE of a conversation before")
print("  ContextEditingMiddleware was added.")
print()
print("  That is lesson 11.")


# ======================================================================
rule("THE SAME QUESTION VIA create_agent")
# ======================================================================
print("The framework version of everything above:")
print()
print("    graph = create_agent(model, tools=tools,")
print("                         system_prompt=...,")
print("                         checkpointer=checkpointer,")
print("                         middleware=[context_editing])")
print("    result = await graph.ainvoke({'messages': [HumanMessage(...)]},")
print("                                config={'configurable':")
print("                                        {'thread_id': session_id}})")
print()
print("It builds a LangGraph state machine with two nodes:")
print("""
         +----------+
    ----->  model   +---- no tool calls ----> END
         +----+-----+
              | tool_calls
         +----v-----+
         |  tools   +----------+
         +----------+          |
              ^                |
              +----------------+
""")
print("The SAME loop, expressed as data rather than control flow. That")
print("is what buys you:")
print()
print("  - snapshot state after every step  -> the checkpointer, and")
print("    therefore persistent conversations           (lesson 11)")
print("  - insert middleware at defined points -> ContextEditingMiddleware")
print("  - stream intermediate steps to a UI")
print("  - interrupt and resume -> human-in-the-loop tool approval")
print("  - add nodes for branching, routing, sub-agents")
print()
print("You cannot retrofit resumable, inspectable state onto a while loop")
print("without essentially rebuilding LangGraph. ** That is the real")
print("argument for it - not the loop, which you just wrote. **")

# Run it for real so the comparison is not hypothetical.
try:
    import asyncio

    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage
    from langchain_core.tools import tool as lc_tool
    from langchain_groq import ChatGroq
    from langgraph.checkpoint.memory import InMemorySaver

    @lc_tool("search_travel_knowledge_base")
    def kb_tool(query: str, destination: str = "",
                categories: list[str] = [], k: int = 5) -> str:
        """Search the travel knowledge base for destination facts.

        The only permitted source of destination facts. Pass `destination`
        for the place in question. Not for weather or exchange rates.
        """
        return json.dumps(
            search_travel_knowledge_base(query, destination, categories, k)
        )

    graph = create_agent(
        ChatGroq(model=MODEL, api_key=KEY, temperature=0, max_retries=5),
        tools=[kb_tool],
        system_prompt=SYSTEM_PROMPT,
        checkpointer=InMemorySaver(),
    )

    print()
    print("  running it for real, with the same KB tool:")
    out = asyncio.run(graph.ainvoke(
        {"messages": [HumanMessage(
            content="What neighbourhoods should I explore in Kyoto?"
        )]},
        config={"configurable": {"thread_id": "learn-10"}},
    ))
    print()
    print(f"  message types in the final state:")
    for message in out["messages"]:
        label = type(message).__name__
        detail = ""
        if label == "AIMessage" and getattr(message, "tool_calls", None):
            detail = " -> " + ", ".join(
                c["name"] for c in message.tool_calls
            )
        print(f"    {label}{detail}")
    print()
    print("  ^ the same message sequence the hand-written loop built, just")
    print("    as objects instead of dicts, and now snapshotted in the")
    print("    checkpointer after every step.")
except Exception as exc:
    print()
    print(f"  (create_agent comparison skipped: {type(exc).__name__}: "
          f"{str(exc)[:160]})")


# ======================================================================
rule("SUMMARY")
# ======================================================================
for server in SERVERS.values():
    server.close()

print("""
  AN AGENT IS A WHILE LOOP.

      while the model asks for a tool:
          run it, append the result, ask again

  What makes it useful is not intelligence - it is that step N+1 can
  depend on the RESULT of step N. The flagship scenario above is the
  whole argument: the indoor search happens because the forecast came
  back poor, and that fact did not exist when the question was asked.

  A keyword router cannot express that. That is why there is no
  hand-written intent classifier in app/agent.py, and the docstring
  says so explicitly.

  What a framework adds:
    - bounded iterations, concurrent tools
    - state snapshots  -> persistence, resume     (lesson 11)
    - middleware       -> context trimming        (lesson 11)
    - streaming, interrupts, branching

  If you were building this again: START WITH THE WHILE LOOP. Add
  LangGraph the day you need persistence or human-in-the-loop. You
  will understand what it is doing, because you will have written
  the thing it replaces.

Next: learn/11-memory/README.md
""")

