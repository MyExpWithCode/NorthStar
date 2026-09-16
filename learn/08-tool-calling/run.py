"""Lesson 08 - the four-step tool-calling dance, done by hand.

    .venv/Scripts/python.exe learn/08-tool-calling/run.py

Needs GROQ_API_KEY in .env. Imports nothing from app/.
"""

import json
import sys
import urllib.error
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared import GROQ_BASE, groq_key, post_json, resolve_model, rule

KEY = groq_key()
MODEL = resolve_model(KEY)
URL = f"{GROQ_BASE}/chat/completions"

print(f"model: {MODEL}")


def show(label: str, obj) -> None:
    print(f"  {label}")
    for line in json.dumps(obj, indent=2).splitlines():
        print("      " + line)


# ======================================================================
rule("THE TOOLS WE WILL OFFER - hand-written JSON Schema")
# ======================================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather_forecast",
            "description": (
                "Get the weather forecast for a city, up to 16 days ahead. "
                "Use for questions about rain, temperature or whether an "
                "outdoor plan is sensible. Not for destination facts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City name, e.g. 'Singapore'",
                    },
                    "days": {
                        "type": "integer",
                        "description": "How many days ahead, 1-16",
                    },
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_travel_knowledge_base",
            "description": (
                "Search the travel knowledge base for destination facts: "
                "attractions, neighbourhoods, transport, food, culture, "
                "opening hours, prices and itineraries. The ONLY permitted "
                "source of destination facts - do not answer destination "
                "questions from your own knowledge. Not for weather or "
                "exchange rates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "What to search for"},
                    "destination": {
                        "type": "string",
                        "description": (
                            "The place the question is about, e.g. "
                            "'Singapore'. Always pass this when the question "
                            "names or implies a destination."
                        ),
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional filter: attractions, transport, food, "
                            "culture, practical, itinerary, shopping, "
                            "accommodation, indoor, outdoor. [] searches all."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
]

print("Two functions, described as JSON Schema. This is the entire")
print("vocabulary the model has for our capabilities.")
print()
show("tool 1 (abridged)", {
    "name": TOOLS[0]["function"]["name"],
    "description": TOOLS[0]["function"]["description"],
    "parameters": TOOLS[0]["function"]["parameters"],
})
print()
print("  ** The 'description' fields are PROMPT ENGINEERING. **")
print("     They are the only thing the model knows about your")
print("     function. A vague description is a bug, and it shows up as")
print("     the model choosing the wrong tool - which looks like a model")
print("     problem and is actually a writing problem.")


# ======================================================================
rule("STEP 1+2 - ask, and watch the model request a tool")
# ======================================================================

messages = [
    {"role": "system",
     "content": "You are a travel assistant. Use the tools provided."},
    {"role": "user",
     "content": "Will it rain in Singapore over the next 3 days?"},
]

response = post_json(URL, {
    "model": MODEL,
    "messages": messages,
    "tools": TOOLS,
    "temperature": 0,
}, KEY)

choice = response["choices"][0]
print(f"  finish_reason : {choice['finish_reason']!r}")
print(f"  content       : {choice['message'].get('content')!r}")
print()
print("  ^ content is None/empty. There is NO ANSWER YET. Print it and")
print("    you get nothing. This is trip-up number one.")
print()
show("the tool_calls the model returned", choice["message"].get("tool_calls"))

call = choice["message"]["tool_calls"][0]
raw_arguments = call["function"]["arguments"]

print()
print(f"  arguments type : {type(raw_arguments).__name__}")
print(f"  arguments repr : {raw_arguments!r}")
print()
print("  ^ it is a STRING, not an object. JSON inside JSON. You must")
print("    json.loads() it, and it can be malformed because a language")
print("    model generated it. Trip-up number two.")

parsed = json.loads(raw_arguments)
print()
print(f"  parsed         : {parsed}")
print(f"  call id        : {call['id']}")
print()
print("  ^ that id must come back with your result or the provider")
print("    rejects the next request. Trip-up number three.")


# ======================================================================
rule("STEP 3 - YOUR code runs the function. The model is just waiting.")
# ======================================================================


def get_weather_forecast(city: str, days: int = 3) -> dict:
    """A fake forecast. Deterministic, so the lesson is reproducible."""
    return {
        "ok": True,
        "source": "learn/08 fake data",
        "location": city,
        "forecast": [
            {"date": "2026-09-17", "high_c": 31, "rain_mm": 0.2,
             "conditions": "partly cloudy", "outdoor_suitability": "good"},
            {"date": "2026-09-18", "high_c": 29, "rain_mm": 24.0,
             "conditions": "heavy rain", "outdoor_suitability": "poor"},
            {"date": "2026-09-19", "high_c": 30, "rain_mm": 1.1,
             "conditions": "light showers", "outdoor_suitability": "fair"},
        ][:days],
    }


REGISTRY = {"get_weather_forecast": get_weather_forecast}

print("  The model asked. It cannot act. It has no network, no")
print("  filesystem, no ability to execute anything.")
print()
print("  ** It is asking PERMISSION, not taking action. **")
print()
print("  So we look up the name in a dict and call it:")
print()
print("      function = REGISTRY[call['function']['name']]")
print("      result = function(**json.loads(call['function']['arguments']))")
print()
result = REGISTRY[call["function"]["name"]](**parsed)
show("what our function returned", result)


# ======================================================================
rule("STEP 4 - hand the result back as a NEW message")
# ======================================================================

messages.append(choice["message"])           # the model's tool request
messages.append({                            # our reply to it
    "role": "tool",
    "tool_call_id": call["id"],
    "content": json.dumps(result),
})

print("  the conversation is now four messages:")
for i, message in enumerate(messages):
    role = message["role"]
    if role == "assistant" and message.get("tool_calls"):
        summary = (
            "tool_calls -> "
            + ", ".join(c["function"]["name"] for c in message["tool_calls"])
        )
    else:
        summary = str(message.get("content"))[:64].replace("\n", " ")
    print(f"    [{i}] {role:<10} {summary}")

final = post_json(URL, {
    "model": MODEL,
    "messages": messages,
    "tools": TOOLS,
    "temperature": 0,
}, KEY)

print()
print(f"  finish_reason : {final['choices'][0]['finish_reason']!r}")
print()
print("  the answer, finally:")
print()
for line in (final["choices"][0]["message"].get("content") or "").splitlines():
    print(f"      {line}")

print()
print("  ** That is the whole mechanism. Four messages and a dict. **")


# ======================================================================
rule("PROOF THAT THE MODEL CANNOT TELL - we lie to it")
# ======================================================================

print("Same question, same tool call. But this time our 'tool' returns")
print("something absurd, and we see whether the model notices:")
print()

lie = {
    "ok": True,
    "source": "learn/08 deliberate lie",
    "location": "Singapore",
    "forecast": [
        {"date": "2026-09-17", "high_c": -40, "rain_mm": 0,
         "conditions": "heavy snow", "outdoor_suitability": "poor"},
    ],
}

lying_messages = [
    messages[0],
    messages[1],
    choice["message"],
    {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(lie)},
]
lied_to = post_json(URL, {
    "model": MODEL, "messages": lying_messages,
    "tools": TOOLS, "temperature": 0,
}, KEY)

print(f'  we returned : "heavy snow, -40C, Singapore"')
print()

lied_message = lied_to["choices"][0]["message"]
lied_content = lied_message.get("content")
lied_calls = lied_message.get("tool_calls") or []

if lied_content:
    print("  the model says:")
    for line in lied_content.splitlines()[:8]:
        print(f"      {line}")
    print()
    print("  It accepted our fabricated data and reported on it. Whatever")
    print("  you put in that message IS reality as far as the model is")
    print("  concerned - it has no way to check.")
elif lied_calls:
    print("  the model did NOT write prose. It asked for another tool call:")
    for call in lied_calls:
        print(f"      {call['function']['name']}"
              f"({call['function']['arguments'][:70]})")
    print()
    print("  Interesting - and a better outcome than pure credulity. Faced")
    print("  with implausible data it chose to re-fetch rather than report.")
    print()
    print("  But be careful what you conclude. It did not 'detect a lie'.")
    print("  It hit a branch where calling the tool again looked like the")
    print("  best next action. Run it a few times: the behaviour varies,")
    print("  and on an earlier run the model cheerfully answered")
    print("  'no rain expected, the weather looks clear' - reporting our")
    print("  -40C snow as fine.")
else:
    print(f"  unexpected reply shape: {list(lied_message)}")

print()
print("  ** The load-bearing point is unchanged, and it does not depend")
print("     on how the model reacted: ** nothing in the protocol lets the")
print("     model verify a tool result. There is no signature, no")
print("     provenance, no second source. If it sometimes pushes back,")
print("     that is a behavioural tendency, not a guarantee.")
print()
print("  Two conclusions, and they point in opposite directions:")
print()
print("   1. GOOD: your code sees every tool result before the model")
print("      does. You can sanitise, log, rate-limit or refuse. This is")
print("      exactly why lesson 06's provenance guarantee holds - the")
print("      citation comes from YOUR artifact, not the model's text.")
print()
print("   2. BAD: a compromised or buggy tool can say anything and the")
print("      model will relay it confidently. Tool output is UNTRUSTED")
print("      INPUT that goes straight into your prompt. If a tool ever")
print("      returns attacker-controlled text, that is prompt injection")
print("      with a direct path in.")


# ======================================================================
rule("@tool - what langchain-core automates")
# ======================================================================

from typing import Annotated

from langchain_core.tools import tool


@tool("search_travel_knowledge_base", response_format="content_and_artifact")
def search_travel_knowledge_base(
    query: str,
    destination: Annotated[
        str,
        "The place the question is about, e.g. 'Singapore'. Always pass this "
        "when the question names or implies a destination.",
    ] = "",
    categories: Annotated[
        list[str],
        "Optional filter: attractions, transport, food, culture, practical, "
        "itinerary, shopping, accommodation, indoor, outdoor. [] = all.",
    ] = [],
    k: Annotated[int, "How many excerpts to return. 0 means the default 5."] = 0,
) -> tuple[str, dict]:
    """Search the travel knowledge base for destination facts.

    The only permitted source of destination facts: attractions,
    neighbourhoods, transport, food, culture, practical tips, opening
    hours, prices and itineraries. Do not answer destination questions
    from your own knowledge. Not for weather or exchange rates.
    """
    content = "[S1] Wikivoyage: Singapore/Bugis > Eat > Budget (relevance 0.71)\n..."
    artifact = {
        "sources": [{
            "marker": "S1",
            "title": "Wikivoyage: Singapore/Bugis",
            "url": "https://en.wikivoyage.org/wiki/Singapore/Bugis",
            "section_path": "Wikivoyage: Singapore/Bugis > Eat > Budget",
            "license": "CC BY-SA 4.0",
            "chunk_id": "wikivoyage-singapore-bugis#0021",
            "score": 0.71,
        }],
    }
    return content, artifact


print("The decorator reads your type hints, Annotated descriptions,")
print("defaults and docstring, and generates this:")
print()
from langchain_core.utils.function_calling import convert_to_openai_tool

generated = convert_to_openai_tool(search_travel_knowledge_base)
show("generated schema", generated)

print()
print("  Compare that to the hand-written version at the top of this")
print("  script. Identical shape, zero maintenance, and it cannot drift")
print("  out of sync with the function signature.")
print()
print("  ** That is the single highest-value thing langchain-core does")
print("     in this project. **")


# ======================================================================
rule("content_and_artifact - the provenance mechanism")
# ======================================================================

invoked = search_travel_knowledge_base.invoke({
    "type": "tool_call",
    "name": "search_travel_knowledge_base",
    "args": {"query": "cheap food", "destination": "Singapore"},
    "id": "call_demo",
})

print(f"  returned a {type(invoked).__name__}")
print()
print("  what the MODEL sees (message.content):")
print(f"      {invoked.content!r}")
print()
print("  what only YOUR CODE sees (message.artifact):")
for line in json.dumps(invoked.artifact, indent=2).splitlines():
    print("      " + line)
print()
print("  The artifact NEVER enters the prompt. So when agent.py builds")
print("  the citation panel it reads the artifact, not the answer text:")
print()
print("      artifact = message.artifact       # {'sources': [...]}")
print()
print("  ** Consequence: if [S1] appears in the answer, a real retrieved")
print("     chunk produced it. The model cannot invent a source. **")
print()
print("  It can still MIS-ATTRIBUTE - cite [S1] for a claim [S2]")
print("  supports. That is a weaker guarantee than 'the answer is")
print("  correct', and app/agent.py is honest about being weaker.")
print()
print("  It also saves tokens: note the source URL is in the artifact")
print("  but NOT in the content. It would cost tokens on every call of")
print("  the loop, and the UI already has it.")


# ======================================================================
rule("THE SCAR - why categories is not a Literal enum")
# ======================================================================

print("kb_tool.py has this comment:")
print("""
    Deliberately NOT expressed as a Literal enum in the tool schema.
    Groq validates tool arguments server-side and rejects the whole
    call with a 400 when a model invents a value -- observed with
    "family", which killed the chat turn outright.
""")
print("Let us reproduce it. A schema WITH a strict enum:")
print()

strict = [{
    "type": "function",
    "function": {
        "name": "search_kb",
        "description": "Search travel facts.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "categories": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["attractions", "food", "transport", "indoor"],
                    },
                },
            },
            "required": ["query"],
        },
    },
}]

# Force the model to want a value outside the enum.
forced = [{
    "role": "assistant",
    "content": None,
    "tool_calls": [{
        "id": "call_bad",
        "type": "function",
        "function": {
            "name": "search_kb",
            "arguments": json.dumps({
                "query": "activities for kids",
                "categories": ["family"],      # NOT in the enum
            }),
        },
    }],
}]

try:
    post_json(URL, {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": "Find family activities."},
            *forced,
            {"role": "tool", "tool_call_id": "call_bad",
             "content": '{"ok": true, "excerpts": []}'},
        ],
        "tools": strict,
        "temperature": 0,
    }, KEY)
    print("  The request was ACCEPTED this time.")
    print()
    print("  Provider-side enum enforcement varies by provider and by")
    print("  endpoint, and it changes over time. The scar in kb_tool.py")
    print("  is real and was observed; whether it reproduces today")
    print("  depends on Groq's current validation. The DESIGN LESSON")
    print("  stands either way - see below.")
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", "replace")
    print(f"  HTTP {exc.code}")
    print(f"  {body[:400]}")
    print()
    print("  ** Reproduced. An invented enum value killed the whole")
    print("     request - not just the one argument. **")

print()
print("  The fix in kb_tool.py, and why it is counter-intuitive:")
print()
print("      categories: Annotated[list[str], '...'] = []     # permissive")
print()
print("      known   = [c for c in requested if c in VALID_CATEGORIES]")
print("      unknown = [c for c in requested if c not in VALID_CATEGORIES]")
print("      # ...run the search with `known`, and TELL the model:")
print("      '(Ignored unknown category filter(s): family. Valid: ...)'")
print()
print("  ** Strict validation turned a recoverable mistake into a fatal")
print("     error. Lenient validation plus an informative message let")
print("     the model self-correct on the next loop. **")
print()
print("  That is backwards from normal API design, and it is correct")
print("  here, because the caller is non-deterministic and CAN read")
print("  your error message.")


# ======================================================================
rule("PARALLEL TOOL CALLS - one turn, several requests")
# ======================================================================

multi = post_json(URL, {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "You are a travel assistant. Use tools."},
        {"role": "user", "content": (
            "I am going to Singapore. Will it rain in the next 3 days, and "
            "what indoor attractions are there?"
        )},
    ],
    "tools": TOOLS,
    "temperature": 0,
}, KEY)

calls = multi["choices"][0]["message"].get("tool_calls") or []
print(f"  the model requested {len(calls)} tool call(s) in ONE reply:")
print()
for c in calls:
    print(f"    {c['function']['name']}")
    print(f"      id   {c['id']}")
    print(f"      args {c['function']['arguments']}")
print()
if len(calls) > 1:
    print("  You must run BOTH and append TWO tool messages, each with its")
    print("  own tool_call_id. Miss one and the provider rejects the")
    print("  next request.")
else:
    print("  It asked for one this time - it will often serialise them,")
    print("  fetching the forecast first and deciding what to do next")
    print("  based on the answer. That sequencing is exactly the")
    print("  flagship scenario, and it is why app/agent.py has no")
    print("  hand-written intent router: the RESULT of the weather call")
    print("  determines whether an indoor search is needed.")


# ======================================================================
rule("SUMMARY")
# ======================================================================
print("""
  THE MODEL NEVER CALLS ANYTHING.

    1. you send    question + JSON schemas of your functions
    2. model sends {"name": "...", "arguments": "{...}"}   (content=null)
    3. YOUR CODE   runs the function
    4. you send    role:"tool", tool_call_id:..., content:...
       -> repeat from 2, or the model writes prose

  Things to remember:
    - content is null on a tool call
    - arguments is a STRING of JSON, and may be malformed
    - tool_call_id must round-trip
    - descriptions ARE prompt engineering
    - tool output is UNTRUSTED INPUT going into your prompt
    - be LENIENT with arguments and explain what you ignored

  The next lesson takes these same tools and moves them behind a
  protocol, so a different program - or someone else's program -
  can serve them.

Next: learn/09-mcp-protocol/README.md
""")
