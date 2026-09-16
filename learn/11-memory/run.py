"""Lesson 11 - memory from a python list up to the real SQLite file.

    .venv/Scripts/python.exe learn/11-memory/run.py

No API key needed. Reads data/conversations.sqlite3 read-only.
Imports nothing from app/.
"""

import json
import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared import rule

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "conversations.sqlite3"


def tokens(text: str) -> int:
    """Rough token estimate. ~4 chars per token for English."""
    return len(text) // 4


# ======================================================================
rule("THE STARTING FACT - from lesson 07")
# ======================================================================
print("""
  The model is STATELESS. It remembers nothing between requests.

  So "conversation memory" is not a model feature. It is entirely
  your problem, and the whole concept is one line:

      ** memory = you kept the message list and re-sent it **

  Everything else in this lesson is storage engineering.
""")


# ======================================================================
rule("LEVEL 1 - a python list, and the growth problem")
# ======================================================================

SYSTEM = (
    "You are a travel planning assistant. Every statement must come from "
    "the knowledge base or a live tool. Never answer a destination question "
    "from your own knowledge. Cite excerpts by their [S1] markers. "
) * 3  # app/prompts.py is ~2,600 chars; this approximates it

# A realistic RAG turn: question, 5 retrieval excerpts, answer.
EXCERPT = "x" * 520          # EXCERPT_CHAR_LIMIT in kb_tool.py
TURNS = [
    ("What neighbourhoods should I explore in Singapore?", 5),
    ("Where should I eat in Chinatown?", 5),
    ("Will it rain on Thursday, and what should I do if it does?", 10),
    ("How much is 50,000 rupees in Singapore dollars?", 0),
    ("Can you put all of that into a 3-day itinerary?", 5),
]

messages: list[dict] = [{"role": "system", "content": SYSTEM}]
print(f"  system prompt: {len(SYSTEM):,} chars (~{tokens(SYSTEM)} tokens)")
print()
print(f"  {'turn':<5} {'question':<46} {'history':>9} {'tokens':>8} {'tool %':>7}")
print(f"  {'-' * 5} {'-' * 46} {'-' * 9} {'-' * 8} {'-' * 7}")

history_sizes = []
for number, (question, excerpts) in enumerate(TURNS, 1):
    messages.append({"role": "user", "content": question})
    if excerpts:
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": [{"id": f"c{number}"}]})
        messages.append({"role": "tool", "tool_call_id": f"c{number}",
                         "content": "\n\n".join([EXCERPT] * excerpts)})
    messages.append({"role": "assistant",
                     "content": "Here is what the guide says... " * 12})

    total = sum(len(json.dumps(m)) for m in messages)
    tool_chars = sum(
        len(json.dumps(m)) for m in messages if m.get("role") == "tool"
    )
    history_sizes.append(total)
    print(f"  {number:<5} {question[:46]:<46} {len(messages):>9} "
          f"{tokens(str(total)) * 0 + total // 4:>8} "
          f"{tool_chars / total:>6.0%}")

print()
print("  the growth curve:")
print()
peak = max(history_sizes)
for number, size in enumerate(history_sizes, 1):
    bar = "#" * int(size / peak * 50)
    print(f"    turn {number}  {size // 4:>6} tokens  {bar}")

print()
print("  ** Cost per turn is not constant - it is CUMULATIVE. ** Every")
print("     turn re-sends everything before it, and you pay again.")
print()
print(f"  Note the 'tool %' column. Retrieval excerpts are ~"
      f"{tool_chars / total:.0%} of the")
print("  payload. THEY are the problem, not the conversation.")
print()
print("  Groq's free tier here allows a few thousand tokens per minute.")
print("  app/agent.py records what happened without trimming:")
print()
print("      'Left alone, the third turn of a conversation gets")
print("       rejected as too large.'")
print()
print("  Not theoretical. HTTP 413.")


# ======================================================================
rule("LEVEL 2 - clear old tool results, by hand")
# ======================================================================

TRIGGER = 4000        # settings.context_trim_trigger_tokens
KEEP = 3              # settings.context_trim_keep_results
PLACEHOLDER = ("[earlier tool result cleared to save context; "
               "search again if you need it]")


def clear_old_tool_uses(history: list[dict]) -> tuple[list[dict], int]:
    """ClearToolUsesEdit, reimplemented. ~12 lines.

    Only fires above the trigger. Keeps the KEEP most recent tool
    results so the CURRENT turn always still has its evidence.
    """
    size = sum(len(json.dumps(m)) for m in history) // 4
    if size < TRIGGER:
        return history, 0

    tool_indices = [
        i for i, m in enumerate(history) if m.get("role") == "tool"
    ]
    to_clear = tool_indices[:-KEEP] if len(tool_indices) > KEEP else []

    trimmed = []
    for i, message in enumerate(history):
        if i in to_clear:
            trimmed.append({**message, "content": PLACEHOLDER})
        else:
            trimmed.append(message)
    return trimmed, len(to_clear)


before = sum(len(json.dumps(m)) for m in messages) // 4
trimmed, cleared = clear_old_tool_uses(messages)
after = sum(len(json.dumps(m)) for m in trimmed) // 4

print(f"  trigger : {TRIGGER} tokens   (history is {before})")
print(f"  keep    : {KEEP} most recent tool results")
print()
print(f"  cleared {cleared} tool result(s)")
print(f"  {before:,} tokens -> {after:,} tokens "
      f"({(before - after) / before:.0%} smaller)")
print()
print("  what survives, message by message:")
print()
for i, message in enumerate(trimmed):
    role = message["role"]
    content = str(message.get("content") or "")
    if content == PLACEHOLDER:
        mark = "CLEARED"
    elif role == "tool":
        mark = "kept"
    else:
        mark = ""
    print(f"    [{i:>2}] {role:<10} {len(content):>6} chars  {mark}")

print()
print("  Three decisions in that, each worth noticing:")
print()
print("  1. It clears TOOL RESULTS, not messages. The questions and")
print("     answers stay - they are small and carry the thread. The")
print("     3 KB retrieval excerpts go. Exactly the right thing to")
print("     drop: high volume, low value after use.")
print()
print(f"  2. It keeps the {KEEP} MOST RECENT. Clear everything and the")
print("     model loses the excerpts it is mid-way through citing.")
print()
print("  3. The placeholder is an INSTRUCTION:")
print(f"       {PLACEHOLDER!r}")
print("     so the model can recover the information rather than being")
print("     silently blinded. A bare '[removed]' would be strictly worse.")
print()
print("  And on the threshold, from app/config.py:")
print()
print("      'The library default is 100,000, which is meaningless")
print("       against an 8,000 tokens-per-minute budget.'")
print()
print("  ** A library default tuned for a paid tier is actively wrong")
print("     on a free one. ** 100,000 would never fire before Groq")
print("     rejected the request.")


# ======================================================================
rule("LEVEL 3 - the real checkpointer file")
# ======================================================================

if not DB.is_file():
    print(f"  {DB.name} does not exist yet. Chat once via the app, then")
    print("  re-run this lesson.")
else:
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    print(f"  {DB.name}  ({DB.stat().st_size / 1024:.0f} KB)")
    print()
    print("  The entire persistence API in app/ is these two lines:")
    print()
    print("      graph = create_agent(model, tools=tools,")
    print("                           checkpointer=checkpointer)")
    print("      config = {'configurable': {'thread_id': session_id}}")
    print()
    print("  You never write a SELECT or an INSERT. And note the input is")
    print("  only the NEW message - LangGraph merges it into the loaded")
    print("  state. You are sending a DELTA to a keyed state, not a")
    print("  history. That trips people up.")

    # -- schema --------------------------------------------------------
    print()
    print("  the schema LangGraph created:")
    print()
    for name, sql in connection.execute(
        "select name, sql from sqlite_master where type='table' order by name"
    ):
        columns = [
            line.strip().rstrip(",")
            for line in (sql or "").splitlines()
            if line.strip() and not line.strip().startswith(("CREATE", ")",
                                                             "PRIMARY"))
        ]
        print(f"    {name}")
        for column in columns:
            print(f"        {column}")
        print()

    print("  Note what is NOT there: no `messages` table, no `role`")
    print("  column, no `ts` column. The whole graph state - every")
    print("  message, every tool call - is one serialised BLOB per step.")

    # -- counts --------------------------------------------------------
    rows = list(connection.execute(
        "select thread_id, checkpoint_id, type, checkpoint, metadata "
        "from checkpoints order by checkpoint_id"
    ))
    threads = {r[0] for r in rows}

    print()
    print(f"  checkpoints : {len(rows)}")
    print(f"  threads     : {len(threads)}")
    print(f"  writes      : "
          f"{connection.execute('select count(*) from writes').fetchone()[0]}")
    print()
    if threads:
        print(f"  ** {len(rows)} checkpoints for {len(threads)} "
              f"conversation(s). **")
        print()
        print("  A checkpoint is written after every GRAPH STEP, not every")
        print("  turn. One question that calls two tools writes ~5 of them.")
        print()
        print("  Two consequences:")
        print("    - the file grows faster than you would guess")
        print("    - you get TIME TRAVEL for free. Every intermediate")
        print("      state is addressable, which is what makes")
        print("      interrupt/resume work.")

    # -- the step numbers ---------------------------------------------
    print()
    print("  metadata IS readable JSON, and it carries the step number:")
    print()
    print(f"    {'checkpoint_id':<38} {'step':>5}  source")
    print(f"    {'-' * 38} {'-' * 5}  {'-' * 24}")
    for _, checkpoint_id, _, _, metadata in rows[:12]:
        try:
            meta = json.loads(metadata)
        except (TypeError, ValueError):
            continue
        print(f"    {checkpoint_id:<38} {str(meta.get('step')):>5}  "
              f"{meta.get('source')}")
    if len(rows) > 12:
        print(f"    ... {len(rows) - 12} more")

    # -- the blob ------------------------------------------------------
    print()
    print("  the checkpoint blob itself:")
    print()
    last = rows[-1]
    print(f"    type  : {last[2]}")
    print(f"    size  : {len(last[3]):,} bytes")
    print(f"    first bytes: {last[3][:64]!r}")
    print()
    if last[2] == "msgpack":
        print("    It is MSGPACK, not JSON and not pickle. Worth noting")
        print("    against lesson 05: index.pkl IS a pickle, so loading")
        print("    an untrusted one executes code. msgpack is data only,")
        print("    which is a better choice for something written on")
        print("    every request.")
        print()
        print("    A plain msgpack decoder is not enough, though - LangGraph")
        print("    uses msgpack EXT types to encode LangChain objects. You")
        print("    need its own serializer to read it back:")
        print()
        print("        from langgraph.checkpoint.serde.jsonplus import \\")
        print("            JsonPlusSerializer")
        print("        state = JsonPlusSerializer().loads_typed((type, blob))")
        print()
        try:
            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

            decoded = JsonPlusSerializer().loads_typed((last[2], last[3]))
            print(f"    top-level keys      : {list(decoded.keys())}")
            print(f"    ts                  : {decoded.get('ts')}")
            channels = decoded.get("channel_values") or {}
            print(f"    channel_values keys : {list(channels.keys())}")
            stored = channels.get("messages") or []
            print(f"    messages stored     : {len(stored)}")
            print()
            print("    and there is the actual conversation:")
            print()
            for message in stored[:10]:
                label = type(message).__name__
                content = str(getattr(message, "content", ""))
                content = content.replace("\n", " ")[:56]
                calls = getattr(message, "tool_calls", None) or []
                if calls:
                    content = "-> " + ", ".join(c["name"] for c in calls)
                print(f"      {label:<14} {content}")
            if len(stored) > 10:
                print(f"      ... {len(stored) - 10} more")
            print()
            print("    ^ THERE is the message list from level 1, round")
            print("      tripped through a BLOB. The timestamp lives inside")
            print("      the blob too, which is why the table has no ts")
            print("      column and ordering relies on checkpoint_id...")

            # If the stored conversation contains the bug lessons 05/06
            # diagnosed, point it out - it is much more convincing than
            # a synthetic example.
            hit_bug = [
                m for m in stored
                if type(m).__name__ == "ToolMessage"
                and "NO_RELEVANT_CONTENT" in str(getattr(m, "content", ""))
            ]
            if hit_bug:
                print()
                print("    " + "-" * 62)
                print("    AN ASIDE WORTH NOTICING")
                print("    " + "-" * 62)
                print()
                print("    This is a REAL saved conversation, and it")
                print("    contains a NO_RELEVANT_CONTENT result.")
                print()
                content = str(getattr(hit_bug[0], "content", ""))
                for line in content.splitlines()[:3]:
                    print(f"      {line[:64]}")
                print()
                print("    That is exactly the defect lessons 05 and 06")
                print("    measured: the knowledge base HAS documents for")
                print("    that destination, but a short query cannot clear")
                print("    the 0.60 relevance floor, so the assistant told")
                print("    a real user the topic was not covered.")
                print()
                print("    The checkpointer just handed you a reproduction")
                print("    case. ** Persisted conversations are a debugging")
                print("    asset, not only a product feature. **")
        except Exception as exc:
            print(f"    (could not decode here: {type(exc).__name__}: "
                  f"{str(exc)[:90]})")

    # -- uuid v6 -------------------------------------------------------
    print()
    print("  ...and checkpoint_id is a UUID version 6:")
    print()
    for _, checkpoint_id, *_ in rows[:4]:
        version = checkpoint_id.split("-")[2][0]
        print(f"    {checkpoint_id}   version {version}")
    print()
    print("  v6 is TIME-ORDERED - the timestamp is in the high bits, so")
    print("  lexicographic sort IS chronological sort. That is how")
    print("  `alist` can yield newest-first without a ts column or an")
    print("  index on one. A nice trick.")

    # -- the awkward part ----------------------------------------------
    rule("THE CONSEQUENCE NOBODY MENTIONS - listing conversations")

    print("A UI wants 'recent conversations, newest first, with titles'.")
    print("The checkpointer API is not built for that - it stores state")
    print("PER THREAD, not a conversation index. So app/agent.py does:")
    print()
    print("    async for tup in lister(None, limit=limit * 12):")
    print("        thread_id = tup.config['configurable']['thread_id']")
    print("        messages = tup.checkpoint['channel_values']['messages']")
    print("        # ...group by thread, keep the one with most messages")
    print()
    print("Read that `limit * 12`. To show 40 conversations it scans")
    print("480 checkpoints, then groups in python.")
    print()

    per_thread: dict[str, int] = {}
    for thread_id, *_ in rows:
        per_thread[thread_id] = per_thread.get(thread_id, 0) + 1

    print("  on THIS database:")
    print()
    print(f"    {'thread_id':<40} {'checkpoints':>12}")
    print(f"    {'-' * 40} {'-' * 12}")
    for thread_id, count in sorted(per_thread.items(),
                                   key=lambda kv: -kv[1]):
        print(f"    {thread_id:<40} {count:>12}")
    print()
    average = len(rows) / max(len(per_thread), 1)
    print(f"  average {average:.1f} checkpoints per conversation, so the")
    print(f"  x12 multiplier would scan {int(40 * 12)} rows to list 40")
    print(f"  conversations - about {40 * 12 / average:.0f}x more than needed.")
    print()
    print("  ** This is the clearest example in the codebase of using a")
    print("     tool slightly outside its design. ** The checkpointer is")
    print("     a STATE STORE, not a conversation index.")
    print()
    print("  The fix is small and boring: your own table")
    print("      (session_id, title, updated_at, turn_count)")
    print("  updated on each turn. Then listing is one indexed query")
    print("  instead of a 480-row scan with a multiplier chosen by feel.")
    print()
    print("  The title instinct in the comment is good product sense,")
    print("  though:")
    print("      'The first user message doubles as the title -- which is")
    print("       what someone scanning a list actually recognises.'")

    connection.close()


# ======================================================================
rule("SQLITE'S ONE HARD LIMIT")
# ======================================================================
print("""
  SQLite is SINGLE-WRITER. Two app instances pointed at one file will
  hit 'database is locked'.

  So AsyncSqliteSaver is:
    - exactly right for one process: one file, zero operations
    - exactly wrong the moment you scale horizontally

  ** That is the migration trigger - not file size. ** Swap in
  PostgresSaver and the application code does not change, which is
  one real thing the checkpointer abstraction buys you.
""")


# ======================================================================
rule("THE FOUR KINDS OF MEMORY THIS IS NOT")
# ======================================================================
print("""
  "Memory" is overloaded. Be precise about which one you mean:

    conversation history   re-send the messages          <- THIS APP
    summarised history     an LLM compresses old turns        no
    semantic / long-term   facts about the user in a
                           vector store, retrieved per turn   no
    user profile           structured preferences in a
                           normal table                       no
    cross-session          "last time you preferred
                           budget hotels"                     no

  NorthStar has conversation history and nothing else. Start a new
  conversation and it knows nothing about you.

  That is a reasonable scope choice. It is worth knowing because when
  someone says "add memory" they usually mean one of the other four,
  and none of them is a checkpointer.

Next: learn/12-serving/README.md
""")
