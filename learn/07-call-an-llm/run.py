"""Lesson 07 - one LLM call, four ways, with the raw JSON shown.

    .venv/Scripts/python.exe learn/07-call-an-llm/run.py

Needs GROQ_API_KEY in .env. Imports nothing from app/.
"""

import json
import sys
import urllib.error
import urllib.request
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared import GROQ_BASE, USER_AGENT, get_json, groq_key, post_json, rule

KEY = groq_key()

SYSTEM = "You are a travel assistant. Answer in one short sentence."
QUESTION = "What currency does Singapore use?"


# ======================================================================
rule("THE LIVE MODEL CATALOGUE - why app/llm.py does not hardcode an id")
# ======================================================================

catalogue = get_json(f"{GROQ_BASE}/models", KEY)
models = sorted(m["id"] for m in catalogue.get("data", []))

NOT_CHAT = ("whisper", "orpheus", "prompt-guard", "safeguard", "tts",
            "embed", "guard")

print(f"GET {GROQ_BASE}/models  ->  {len(models)} models")
print()
for model_id in models:
    low = model_id.lower()
    if any(marker in low for marker in NOT_CHAT):
        note = "dropped: not a chat model"
    elif low.startswith("groq/compound"):
        note = "dropped: agentic system with its OWN built-in tools"
    else:
        note = "usable for chat + tool calling"
    print(f"  {model_id:<34} {note}")

print()
print("Note what is NOT in that list: any 'llama-3.3-*' id. Most tutorials")
print("still name one. They are already broken. That is exactly why")
print("app/llm.py resolves the model from the LIVE catalogue at startup")
print("rather than pinning a string.")
print()
print("The 'groq/compound' exclusion is the subtle one: those models come")
print("with their own tools. This app supplies its own and does its own")
print("tool selection, so mixing them would destroy the provenance")
print("guarantee from lesson 06 - you could not say which tool produced")
print("which fact.")

PREFERRED = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b")
MODEL = next((m for m in PREFERRED if m in models), None)
if MODEL is None:
    MODEL = next(
        m for m in models
        if not any(x in m.lower() for x in NOT_CHAT)
        and not m.startswith("groq/compound")
    )
print()
print(f"resolved model: {MODEL}")


# ======================================================================
rule("THE GOTCHA - a valid key, and still HTTP 403")
# ======================================================================

print("First, the raw call WITHOUT a User-Agent header:")
print()
bad = urllib.request.Request(
    f"{GROQ_BASE}/models",
    headers={"Authorization": f"Bearer {KEY}"},     # no User-Agent
)
try:
    urllib.request.urlopen(bad, timeout=20)
    print("  ...it worked. Groq's edge behaviour may have changed.")
except urllib.error.HTTPError as exc:
    print(f"  HTTP {exc.code} {exc.reason}")
    print()
    print("  Same key. Same URL. The ONLY difference is that python sent")
    print("  its default 'User-Agent: Python-urllib/3.x', which the edge")
    print("  in front of the API rejects. No auth error, no useful body.")
    print()
    print("  You never see this through the SDK, because groq (via httpx)")
    print("  sets a sensible User-Agent for you.")
    print()
    print("  ** That is a concrete thing a client library buys you: a")
    print("     class of infrastructure failure you never learn exists. **")
    print("     Worth remembering next time a dependency looks like")
    print("     pure overhead. (Lesson 02 hit the identical problem")
    print("     with Wikimedia.)")


# ======================================================================
rule("WAY 1 - raw urllib. No SDK. ~10 lines.")
# ======================================================================

payload = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": QUESTION},
    ],
    "temperature": 0,
}

print("the request body we are POSTing:")
print()
for line in json.dumps(payload, indent=2).splitlines():
    print("    " + line)

print()
print(f"POST {GROQ_BASE}/chat/completions")
response = post_json(f"{GROQ_BASE}/chat/completions", payload, KEY)

print()
print("the FULL unedited response:")
print()
for line in json.dumps(response, indent=2).splitlines():
    print("    " + line)

print()
print("the answer is one field, buried:")
print("    response['choices'][0]['message']['content']")
print()
print(f"  -> {response['choices'][0]['message']['content']!r}")

usage = response.get("usage", {})
print()
print("and the bit you should always look at:")
print(f"    prompt_tokens     {usage.get('prompt_tokens')}")
print(f"    completion_tokens {usage.get('completion_tokens')}")
print(f"    total_tokens      {usage.get('total_tokens')}")
print()
print("Remember that number. Lesson 11 is entirely about it growing.")


# ======================================================================
rule("STATELESS - the single most important property")
# ======================================================================

print("Ask a follow-up that only makes sense with context, sending NO")
print("history at all:")
print()
follow_up = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "And how much is that in rupees?"}],
    "temperature": 0,
}
result = post_json(f"{GROQ_BASE}/chat/completions", follow_up, KEY)
print(f'  user  : "And how much is that in rupees?"')
print(f'  model : "{result["choices"][0]["message"]["content"][:220]}"')
print()
print("It has no idea what 'that' is, because THE API IS STATELESS.")
print()
print("Now send the history explicitly:")
print()
with_history = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": QUESTION},
        {"role": "assistant",
         "content": response["choices"][0]["message"]["content"]},
        {"role": "user", "content": "And how much is that in rupees?"},
    ],
    "temperature": 0,
}
result2 = post_json(f"{GROQ_BASE}/chat/completions", with_history, KEY)
print(f'  model : "{result2["choices"][0]["message"]["content"][:220]}"')
print()
print("** The model remembers NOTHING. If a conversation appears to have")
print("   memory, it is because YOU re-sent the whole history. **")
print()
print(f"  turn 1 prompt_tokens : {usage.get('prompt_tokens')}")
print(f"  turn 2 prompt_tokens : {result2.get('usage', {}).get('prompt_tokens')}")
print()
print("Every turn re-sends everything, and you pay for it again. That is")
print("why app/config.py has context_trim_trigger_tokens. Lesson 11.")


# ======================================================================
rule("WAY 2 - the groq SDK")
# ======================================================================

try:
    from groq import Groq

    client = Groq(api_key=KEY)
    sdk_response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": QUESTION},
        ],
        temperature=0,
    )
    print("    client = Groq(api_key=KEY)")
    print("    response = client.chat.completions.create(")
    print("        model=MODEL, messages=[...], temperature=0)")
    print()
    print(f"  -> {sdk_response.choices[0].message.content!r}")
    print()
    print(f"  return type: {type(sdk_response).__name__}  (a typed object,")
    print("               not a dict - so attribute access and")
    print("               autocomplete work)")
    print()
    print("  what it added over way 1:")
    print("    - a working User-Agent (see the gotcha above)")
    print("    - typed response objects")
    print("    - automatic retries with backoff on 429/5xx")
    print("    - streaming, pagination, timeouts")
except ImportError:
    print("  groq SDK not installed (it arrives as a langchain-groq dep)")


# ======================================================================
rule("WAY 3 - LangChain ChatGroq, which is what app/llm.py builds")
# ======================================================================

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

chat = ChatGroq(model=MODEL, api_key=KEY, temperature=0, max_retries=5)
lc_response = chat.invoke(
    [SystemMessage(content=SYSTEM), HumanMessage(content=QUESTION)]
)

print("    chat = ChatGroq(model=MODEL, api_key=KEY, temperature=0)")
print("    response = chat.invoke([SystemMessage(...), HumanMessage(...)])")
print()
print(f"  -> {lc_response.content!r}")
print()
print(f"  return type: {type(lc_response).__name__}")
print()
print("  the same three roles, as classes:")
print("    SystemMessage  ==  {'role': 'system', ...}")
print("    HumanMessage   ==  {'role': 'user', ...}")
print("    AIMessage      ==  {'role': 'assistant', ...}")
print()
print("  usage is attached as metadata:")
for key_name, value in (lc_response.usage_metadata or {}).items():
    if not isinstance(value, dict):
        print(f"    {key_name:<22} {value}")

print()
print("  what it added over way 2:")
print("    - Message classes that LangGraph, the checkpointer and")
print("      LangSmith all understand")
print("    - .bind_tools(): python functions -> JSON schemas (lesson 08)")
print("    - one interface across providers. app/llm.py swaps ChatGroq")
print("      for ChatAnthropic and NOTHING else changes - which is how")
print("      it supports two providers in about 40 lines.")


# ======================================================================
rule("WAY 4 - temperature, demonstrated")
# ======================================================================

print("app/llm.py hardcodes temperature=0, with a good reason in the")
print("docstring. Here is the reason, measured.")
print()
creative = "Invent a name for a hawker stall selling chicken rice."

for temperature in (0, 1.2):
    print(f"  temperature = {temperature}")
    answers = []
    for _ in range(3):
        out = post_json(f"{GROQ_BASE}/chat/completions", {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "Reply with the name only."},
                {"role": "user", "content": creative},
            ],
            "temperature": temperature,
        }, KEY)
        answers.append(out["choices"][0]["message"]["content"].strip()[:60])
    for i, answer in enumerate(answers, 1):
        print(f"    run {i}: {answer}")
    print(f"    distinct: {len(set(answers))}/3")
    print()

print("  temperature=0 is what you want when the model's job is to REPORT")
print("  retrieved facts and tool results. Run-to-run variation in what it")
print("  CLAIMS is a liability, not a feature.")
print()
print("  (0 is near-deterministic, not perfectly so - GPU float")
print("   non-determinism and batching still leak through.)")


# ======================================================================
rule("SUMMARY")
# ======================================================================
print("""
  A language model API is a STATELESS FUNCTION:

      list[message] -> one more message

  No sessions. No memory. No connection state. Everything that looks
  like memory is you re-sending history and paying for it again.

  The layers:
    urllib POST        10 lines, one provider, no User-Agent (403!)
    groq SDK           typed, retries, streaming, correct headers
    ChatGroq           Message objects, bind_tools, provider swap

  Next lesson answers the one question that turns this into an agent:
  how does a model "use a tool" when the API only returns text?

  Spoiler: it does not. It returns JSON, and YOUR code does the work.

Next: learn/08-tool-calling/README.md
""")
