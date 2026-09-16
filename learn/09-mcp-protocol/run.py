"""Lesson 09 - speak MCP to the project's real servers, by hand.

    .venv/Scripts/python.exe learn/09-mcp-protocol/run.py

No API key needed. The client side here uses NO mcp library at all -
just subprocess and json - so you can see the actual bytes.
"""

import json
import subprocess
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared import rule

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable

print("""
NOTE: you may see lines like

    [09/16/26 16:24:50] INFO  Processing request of type   server.py:733
                              ListToolsRequest

interleaved with this output. That is the MCP SERVER logging to its own
stderr, which your terminal shows alongside ours.

It is worth understanding rather than ignoring: on a stdio server,
STDOUT IS THE PROTOCOL CHANNEL. A single print() to stdout corrupts the
JSON-RPC stream and the client sees a parse error. So all logging must go
to stderr - which is exactly why both of this project's servers open with

    logging.getLogger("httpx").setLevel(logging.WARNING)

httpx logs every request at INFO, and on a stdio server that noise lands
in the client's captured stderr and buries anything worth reading.

Pipe stderr away to silence it:  ... run.py 2>/dev/null
""")


# ======================================================================
# A complete MCP client. No mcp library. ~40 lines.
# ======================================================================
class RawMcpClient:
    """Speaks JSON-RPC 2.0 over a subprocess's stdin/stdout.

    This is the entire client side of MCP for the stdio transport. Every
    byte is printed so nothing is hidden.
    """

    def __init__(self, module: str, verbose: bool = True) -> None:
        self.verbose = verbose
        self._next_id = 0
        self.process = subprocess.Popen(
            [PYTHON, "-m", module],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
            text=True,
            encoding="utf-8",
            bufsize=1,          # line buffered: the protocol is line-based
        )

    def _send(self, message: dict) -> None:
        line = json.dumps(message)
        if self.verbose:
            print(f"    -> {line[:150]}")
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def _receive(self) -> dict | None:
        line = self.process.stdout.readline()
        if not line:
            return None
        if self.verbose:
            shown = line.strip()
            print(f"    <- {shown[:150]}{'...' if len(shown) > 150 else ''}")
        return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        """A request HAS an id, so a reply is expected."""
        self._next_id += 1
        self._send({
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            **({"params": params} if params is not None else {}),
        })
        return self._receive()

    def notify(self, method: str, params: dict | None = None) -> None:
        """A notification has NO id, so no reply comes back."""
        self._send({
            "jsonrpc": "2.0",
            "method": method,
            **({"params": params} if params is not None else {}),
        })

    def close(self) -> None:
        try:
            self.process.stdin.close()
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()


# ======================================================================
rule("STEP 1 - launch the real currency server as a subprocess")
# ======================================================================

print("    python -m app.mcp_servers.currency_server")
print()
print("stdin and stdout are the protocol channel. Nothing else.")
print()

client = RawMcpClient("app.mcp_servers.currency_server")


# ======================================================================
rule("STEP 2 - the handshake (the part nobody ever sees)")
# ======================================================================

print("  2a. initialize - a REQUEST (it has an id)")
print()
init = client.request("initialize", {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "learn-09", "version": "0.1"},
})
print()
print("  the server's reply, formatted:")
for line in json.dumps(init, indent=2).splitlines():
    print("      " + line)

server_info = (init.get("result") or {}).get("serverInfo", {})
instructions = (init.get("result") or {}).get("instructions", "")
print()
print(f"  serverInfo   : {server_info}")
print(f"  protocol     : {(init.get('result') or {}).get('protocolVersion')}")
if instructions:
    print(f"  instructions : {instructions[:150]}...")
    print("                 ^ set by FastMCP(instructions=...) in the server.")
    print("                   Server-level guidance for the model, separate")
    print("                   from any single tool's description.")

print()
print("  2b. notifications/initialized - a NOTIFICATION (no id, no reply)")
print()
client.notify("notifications/initialized")
print()
print("  ** Skip that notification and the server refuses to continue. **")
print("     It is the handshake completion. This is the single most")
print("     common reason a hand-written MCP client hangs forever.")


# ======================================================================
rule("STEP 3 - tools/list, and a familiar-looking schema")
# ======================================================================

listing = client.request("tools/list")
tools = (listing.get("result") or {}).get("tools", [])

print()
print(f"  the server exposes {len(tools)} tool(s):")
for tool in tools:
    print()
    print(f"    name        : {tool['name']}")
    print(f"    description : {tool.get('description', '')[:160]}")
    print(f"    inputSchema :")
    for line in json.dumps(tool.get("inputSchema"), indent=2).splitlines():
        print("        " + line)

print()
print("  ** That inputSchema is EXACTLY the JSON Schema from lesson 08. **")
print()
print("  MCP did not invent a new way to describe a function. It took the")
print("  existing one and wrapped it in a JSON-RPC envelope. If lesson 08")
print("  made sense, you already understand 90% of MCP.")


# ======================================================================
rule("STEP 4 - tools/call, for real, over the network")
# ======================================================================

print("  converting INR 50,000 to SGD using live ECB reference rates:")
print()
call = client.request("tools/call", {
    "name": "convert_currency",
    "arguments": {
        "amount": 50000,
        "from_currency": "INR",
        "to_currency": "SGD",
    },
})

print()
result = call.get("result") or {}
print("  the result, formatted:")
for line in json.dumps(result, indent=2).splitlines():
    print("      " + line)

# The envelope is inside the text content block.
payload = None
for block in result.get("content") or []:
    if block.get("type") == "text":
        try:
            payload = json.loads(block["text"])
        except (TypeError, ValueError):
            pass

if payload:
    print()
    print("  the ENVELOPE both servers use:")
    print(f"      ok           {payload.get('ok')}")
    print(f"      source       {payload.get('source')}")
    print(f"      retrieved_at {payload.get('retrieved_at')}")
    data = payload.get("data") or {}
    for key, value in data.items():
        print(f"      data.{key:<16} {value}")
    print()
    print("  Note 'rate_date'. ECB rates are published once a business day,")
    print("  so a Sunday conversion uses Friday's number. The tool refuses")
    print("  to let its output look more current than it is - the")
    print("  assistant can say HOW current the figure is instead of")
    print("  implying it is live to the second.")


# ======================================================================
rule("STEP 5 - a failure, reported as DATA rather than an exception")
# ======================================================================

print("  asking for a currency that does not exist:")
print()
bad = client.request("tools/call", {
    "name": "convert_currency",
    "arguments": {"amount": 100, "from_currency": "XYZ", "to_currency": "SGD"},
})
bad_result = bad.get("result") or {}
for block in bad_result.get("content") or []:
    if block.get("type") == "text":
        try:
            envelope = json.loads(block["text"])
            print()
            print(f"      ok     {envelope.get('ok')}")
            print(f"      error  {envelope.get('error')}")
        except (TypeError, ValueError):
            print(f"      {block['text'][:300]}")

print()
print("  It did not raise. It did not guess a plausible-looking number.")
print("  It returned ok=False with a REASON, which the model can relay.")
print()
print("  ** That is lesson 06's 'I don't know' principle applied to a")
print("     live tool. Compare with raising an exception: the turn dies,")
print("     or the model gets a stack trace and improvises. **")
print()
print("  And note the supported-currency list comes from the SERVICE,")
print("  not a hardcoded tuple that silently rots.")

client.close()


# ======================================================================
rule("STEP 6 - the weather server, and the flagship data point")
# ======================================================================

weather = RawMcpClient("app.mcp_servers.weather_server", verbose=False)
weather.request("initialize", {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "learn-09", "version": "0.1"},
})
weather.notify("notifications/initialized")

wtools = (weather.request("tools/list").get("result") or {}).get("tools", [])
print(f"  tools: {', '.join(t['name'] for t in wtools)}")
print()

forecast = weather.request("tools/call", {
    "name": "get_weather_forecast",
    "arguments": {"city": "Singapore", "days": 5},
})
for block in (forecast.get("result") or {}).get("content") or []:
    if block.get("type") != "text":
        continue
    try:
        envelope = json.loads(block["text"])
    except (TypeError, ValueError):
        continue
    data = envelope.get("data") or {}
    print(f"  source       : {envelope.get('source')}")
    print(f"  location     : {data.get('location')}")
    print(f"  retrieved_at : {envelope.get('retrieved_at')}")
    print()
    print(f"    {'date':<12} {'high C':>7} {'rain mm':>8} {'rain %':>7}  "
          f"{'conditions':<20} {'outdoor':<8}")
    print(f"    {'-' * 12} {'-' * 7} {'-' * 8} {'-' * 7}  {'-' * 20} "
          f"{'-' * 8}")
    for day in data.get("forecast") or []:
        print(f"    {str(day.get('date')):<12} "
              f"{str(day.get('temp_max_c')):>7} "
              f"{str(day.get('precipitation_mm')):>8} "
              f"{str(day.get('precipitation_probability_pct')):>7}  "
              f"{str(day.get('conditions'))[:20]:<20} "
              f"{str(day.get('outdoor_suitability')):<8}")
    print()
    print("  ** 'outdoor_suitability' is computed by the SERVER, not the")
    print("     model. ** That is deliberate: it turns a judgement call")
    print("     ('is 24mm of rain a problem?') into a fact the model can")
    print("     act on. It is what makes the flagship scenario work -")
    print("     see a 'poor' day, search the KB for categories=['indoor'].")

weather.close()


# ======================================================================
rule("STEP 7 - the same thing via langchain-mcp-adapters")
# ======================================================================

print("Now the layered version, which is what app/mcp_client.py uses:")
print()
print("    from langchain_mcp_adapters.client import MultiServerMCPClient")
print()
print("    client = MultiServerMCPClient({'currency': {")
print("        'command': sys.executable,")
print("        'args': ['-m', 'app.mcp_servers.currency_server'],")
print("        'transport': 'stdio'}})")
print("    tools = await client.get_tools(server_name='currency')")
print()

import asyncio

from langchain_mcp_adapters.client import MultiServerMCPClient


async def via_adapter() -> None:
    client = MultiServerMCPClient({
        "currency": {
            "command": PYTHON,
            "args": ["-m", "app.mcp_servers.currency_server"],
            "transport": "stdio",
        }
    })
    tools = await client.get_tools(server_name="currency")
    print(f"  got {len(tools)} LangChain tool object(s):")
    for tool in tools:
        print(f"    {tool.name}  ({type(tool).__name__})")
    print()
    print("  ^ these are now ordinary LangChain BaseTool objects. They can")
    print("    go straight into create_agent alongside the local")
    print("    search_travel_knowledge_base tool, and the agent cannot")
    print("    tell which came from where.")
    print()
    print("  ** That is the whole job of langchain-mcp-adapters: translate")
    print("     MCP tool descriptors into LangChain tool objects. **")
    print()
    tool = next(t for t in tools if t.name == "convert_currency")
    out = await tool.ainvoke({
        "amount": 1000, "from_currency": "SGD", "to_currency": "INR",
    })
    print(f"  invoking it returns a {type(out).__name__}:")
    print(f"    {str(out)[:300]}")
    print()
    print("  Note the shape. app/mcp_client.py has parse_tool_payload()")
    print("  precisely because the adapter hands this back in THREE")
    print("  different shapes depending on how the tool was invoked:")
    print("    1. a ToolMessage with artifact['structured_content']")
    print("    2. a list of blocks, each {'type':'text','text':<json>}")
    print("    3. a bare JSON string")
    print()
    print("  That friction is the clearest concrete cost of stacking")
    print("  three abstractions: MCP -> adapter -> LangChain.")


asyncio.run(via_adapter())


# ======================================================================
rule("STEP 8 - degraded mode: one server down, the app still works")
# ======================================================================

print("app/mcp_client.py connects each server SEPARATELY:")
print()
print("    for server_name, module in SERVER_MODULES.items():")
print("        client = MultiServerMCPClient({server_name: ...})")
print("        try:")
print("            tools = await client.get_tools(...)")
print("        except Exception as exc:")
print("            toolset.degraded[server_name] = describe(exc)")
print("            continue")
print()
print("The obvious implementation passes BOTH servers to ONE client - and")
print("then one broken server takes down both. Let us break one:")
print()


async def degraded() -> None:
    servers = {
        "currency": "app.mcp_servers.currency_server",
        "weather": "app.mcp_servers.does_not_exist",     # deliberately broken
    }
    available: dict[str, list[str]] = {}
    down: dict[str, str] = {}

    for name, module in servers.items():
        client = MultiServerMCPClient({
            name: {"command": PYTHON, "args": ["-m", module],
                   "transport": "stdio"},
        })
        try:
            tools = await client.get_tools(server_name=name)
            available[name] = [t.name for t in tools]
        except Exception as exc:
            # Unwrap the ExceptionGroup that anyio task groups produce.
            while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
                exc = exc.exceptions[0]
            down[name] = f"{type(exc).__name__}: {str(exc).strip()[:90]}"

    for name, names in available.items():
        print(f"    {name:<10} UP        {', '.join(names)}")
    for name, reason in down.items():
        print(f"    {name:<10} DOWN      {reason}")

    print()
    print("  The app still starts, and still converts currency.")
    print()
    print("  Note the exception unwrapping. Without it the error reads:")
    print("    'ExceptionGroup: unhandled errors in a TaskGroup")
    print("     (1 sub-exception)'")
    print("  which is true and completely useless. MCP runs each server")
    print("  in an anyio task group, so every startup failure arrives")
    print("  wrapped. Those 3 lines in mcp_client.py turn an")
    print("  unactionable error into an actionable one.")
    print()
    print("  And then the crucial follow-through - the SYSTEM PROMPT is")
    print("  told what is missing:")
    print()
    print("    TOOL AVAILABILITY: the following live-information")
    print("    capabilities are currently unavailable: current conditions")
    print("    and weather forecasts (weather tool unavailable). If the")
    print("    user asks for any of it, say plainly that it cannot be")
    print("    retrieved right now. Never estimate or recall it.")
    print()
    print("  Why both halves matter:")
    print("    - dropping the tool means it CANNOT be hallucinated into a")
    print("      fake result, because it is not in the schema list")
    print("    - but the model would then just answer from memory")
    print("    - naming the gap is what lets it say 'I cannot check the")
    print("      forecast right now' instead")


asyncio.run(degraded())


# ======================================================================
rule("SO IS MCP WORTH IT HERE? - the honest answer")
# ======================================================================
print("""
  AGAINST. These two servers only serve this app. Making them plain
  python functions would delete:
    - two subprocesses and the JSON-RPC layer
    - langchain-mcp-adapters
    - parse_tool_payload's three-shape problem
    - the degraded-mode connection dance
  Roughly 150 lines net. Lesson 13 does exactly that, and it works.

  FOR. MCP buys things that only pay off OUTSIDE this process:
    1. REUSE          the weather server works with Claude Desktop,
                      Cursor, VS Code - any MCP client, unchanged
    2. ISOLATION      a hang or crash in it cannot take the web app
                      down; separate process, separate memory
    3. LANGUAGE       an MCP server can be TypeScript, Go, Rust
    4. OTHER TOOLS    the real payoff. Hundreds of MCP servers
                      already exist - filesystem, Postgres, GitHub,
                      Slack. Speaking MCP means adopting any of them
                      with no integration work
    5. DEPLOYMENT     update the tool without redeploying the app

  ** MCP is a DISTRIBUTION AND INTEROPERABILITY standard, not a
     better way to call a function. **

  If your tools will only ever be used by your own app, you are
  paying for something you are not using. If they might be used by
  anything else - or you want to consume anything else - it is a
  very good deal.

Next: learn/10-agent-loop/README.md
""")
