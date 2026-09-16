# 09 — MCP: what it actually is on the wire

**Real files:** [app/mcp_servers/weather_server.py](../../app/mcp_servers/weather_server.py),
[app/mcp_servers/currency_server.py](../../app/mcp_servers/currency_server.py),
[app/mcp_client.py](../../app/mcp_client.py)
**Libraries:** `mcp[cli]` (FastMCP), `langchain-mcp-adapters`, `httpx`

No API key needed. This lesson talks to the project's real MCP servers.

## The question to hold onto

Lesson 08 showed that a tool is just a python function plus a JSON schema. So:

> **If a tool is just a function, why does NorthStar run its weather and
> currency tools as separate subprocesses speaking a protocol?**

The lesson answers that honestly, including where the answer is "it does not
need to, for this app".

## MCP in one paragraph

**Model Context Protocol** is a specification for how a program exposes tools,
resources and prompts to an LLM application. It is **JSON-RPC 2.0** over a
transport — usually stdin/stdout of a subprocess, or HTTP. That is the whole
idea: a standard wire format so any MCP client can use any MCP server without
either knowing about the other.

It is to LLM tools roughly what LSP (Language Server Protocol) is to editors.
Before LSP, every editor wrote its own Python integration. After, one language
server serves them all. MCP is the same bet.

## The actual bytes

`run.py` launches the real currency server and speaks the protocol by hand —
no MCP library on the client side, just `subprocess` and `json`. Here is the
conversation:

**1. Initialise** (client → server, on stdin, one line of JSON):

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "protocolVersion":"2025-06-18",
  "capabilities":{},
  "clientInfo":{"name":"learn-09","version":"0.1"}}}
```

Server replies on stdout with its capabilities and `serverInfo`. Then the
client must send the `notifications/initialized` notification — a message with
**no `id`**, so no reply is expected. Skip it and the server refuses to
continue. This handshake is the part most people never see.

**2. List tools:**

```json
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```

The reply contains each tool's `name`, `description` and `inputSchema` — and
that `inputSchema` is **exactly the JSON Schema from lesson 08**. MCP did not
invent a new way to describe a function. It wrapped the existing one in an
envelope.

**3. Call one:**

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
  "name":"convert_currency",
  "arguments":{"amount":50000,"from_currency":"INR","to_currency":"SGD"}}}
```

That is it. Three request types and a notification.

## Writing a server: `FastMCP`

```python
from mcp.server.fastmcp import FastMCP

server = FastMCP(name="currency", instructions="...")

@server.tool(description="Convert an amount of money from one currency...")
def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict:
    """Convert `amount` from one ISO 4217 currency to another."""
    ...

if __name__ == "__main__":
    server.run(transport="stdio")
```

`FastMCP` does for MCP what `@tool` does for LangChain: derives the schema from
type hints and docstrings, and handles the JSON-RPC framing. `server.run()` is
a read-a-line / dispatch / write-a-line loop on stdin/stdout.

**One consequence that surprises everyone: on a stdio server you must never
`print()`.** stdout *is* the protocol channel. A stray print corrupts the
stream and the client sees a parse error. Logging must go to stderr — which is
why both servers start with:

```python
logging.getLogger("httpx").setLevel(logging.WARNING)
```

`httpx` logs every request at INFO, and on a stdio server that noise lands in
the client's captured stderr and buries anything worth reading.

## The envelope: the best idea in these two files

Every tool in both servers returns the same shape:

```python
{"ok": True,  "source": ..., "retrieved_at": ..., "data": {...}}
{"ok": False, "source": ..., "retrieved_at": ..., "error": "..."}
```

Four things this buys, and they are all about honesty rather than convenience:

| Field | Why it exists |
|---|---|
| `ok` | a failure is **data**, not an exception. The model is told "this could not be retrieved" and can say so |
| `source` | `"Open-Meteo"`, `"Frankfurter (ECB reference rates)"` — attribution for the UI panel |
| `retrieved_at` | live data has an age. The UI shows it |
| `error` | a *reason*, so the model says why rather than guessing |

Compare with raising an exception: the turn dies, or worse, the model gets a
stack trace and improvises. With the envelope, "the forecast is unavailable" is
a sentence the assistant can actually say. This is lesson 06's "I don't know"
principle applied to live tools.

### Two honesty details worth stealing

From `currency_server.py`:

> **`rate_date` is always surfaced.** ECB reference rates are published once a
> business day, so a Sunday conversion uses Friday's rate.

The tool refuses to let its output look more current than it is. A converted
amount without a rate date implies live-to-the-second accuracy that does not
exist.

> **Unknown currency codes are rejected, never guessed.** The supported list
> comes from the service itself, so "XYZ" produces a clear error rather than a
> plausible-looking number.

Note *from the service itself* — not a hardcoded list that silently rots.

## Fault tolerance: the design detail in `mcp_client.py`

```python
for server_name, module in SERVER_MODULES.items():
    client = MultiServerMCPClient({server_name: _connection(module)})
    try:
        tools = await client.get_tools(server_name=server_name)
    except Exception as exc:
        toolset.degraded[server_name] = _describe_failure(exc)
        continue
```

**One client per server, connected separately.** The obvious implementation
passes both servers to one `MultiServerMCPClient` — and then one broken server
takes down both. Here, if weather fails, currency still works.

Two follow-through details that make this actually useful:

**1. The prompt is told what is missing.**

```python
def prompt_note(self) -> str:
    return ("TOOL AVAILABILITY: the following live-information capabilities "
            "are currently unavailable: ... If the user asks for any of it, "
            "say plainly that it cannot be retrieved right now. Never "
            "estimate or recall it.")
```

A tool absent from the schema list cannot be hallucinated into a fake result —
so degrading the tool list is itself a safety mechanism. But the model would
then just answer from memory. Naming the gap is what lets it say *"I cannot
check the forecast right now"*.

**2. Exception groups are unwrapped.**

```python
while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
    exc = exc.exceptions[0]
```

MCP runs each server in an `anyio` task group, so a server that fails to start
surfaces as `ExceptionGroup: unhandled errors in a TaskGroup (1
sub-exception)` — true and useless. This digs out the real cause. A small
function that turns an unactionable error into an actionable one.

## Three shapes of the same payload

`parse_tool_payload` in `mcp_client.py` exists because the adapter hands the
result back in three different shapes depending on how the tool was invoked:

1. a `ToolMessage` with `artifact["structured_content"]` as a dict
2. a list of MCP content blocks, each `{"type": "text", "text": "<json>"}`
3. a bare JSON string

That is friction from stacking three abstractions (MCP → adapter → LangChain),
and it is the clearest concrete cost of the layering in this project. It
returns `{}` when nothing parses, so an unreadable result becomes "no
provenance" instead of a dead chat turn.

## So: is MCP worth it here?

**The honest case against.** These two servers only serve this app. Making them
plain python functions would delete: two subprocesses, the JSON-RPC layer,
`langchain-mcp-adapters`, `parse_tool_payload`'s three-shape problem, and the
whole degraded-mode connection dance. Probably 150 lines net. Lesson 13 does
exactly that and it works fine.

**The honest case for.** MCP buys things that only pay off outside this
process:

1. **Reuse.** The weather server works with Claude Desktop, Cursor, VS Code, or
   any MCP client, unchanged. A function in `app/` works with `app/`.
2. **Isolation.** A crash or a hang in the weather server cannot take the web
   app down. It is a separate process with a separate memory space.
3. **Language independence.** An MCP server can be TypeScript, Go or Rust.
4. **Someone else's tools.** The real payoff. There are hundreds of existing
   MCP servers — filesystem, Postgres, GitHub, Slack. Speaking MCP means you
   can adopt any of them without writing an integration.
5. **Independent deployment.** Update the weather server without redeploying
   the app.

For a demonstration project, (1) and (4) are the substantive reasons: it shows
the app is built on an open integration surface rather than a closed one. The
server docstring makes the claim explicitly, and it is a fair one:

> any MCP client can connect to it, which is what makes this a real MCP
> integration rather than a local function wearing an MCP label.

**The pragmatic summary:** MCP is a distribution and interoperability standard,
not a better way to call a function. If your tools will only ever be used by
your own app, you are paying for something you are not using. If they might be
used by anything else — or you want to consume anything else — it is a very
good deal.

## Alternatives

### Transports
| Transport | Use |
|---|---|
| **stdio** | **used here.** Subprocess. Simplest, no ports, no auth needed |
| **Streamable HTTP** | remote servers; the current spec's HTTP transport |
| SSE | the older HTTP transport, now deprecated |

### Ways to give a model tools
| Approach | Interop | Isolation | Complexity |
|---|---|---|---|
| plain python functions | none | none | **lowest.** Lesson 13 |
| LangChain `@tool` | LangChain only | none | low |
| **MCP** | **any MCP client** | **process** | medium. **Used here** |
| OpenAPI / REST + a spec-to-tool bridge | any HTTP client | process/network | medium |
| gRPC service | any gRPC client | process/network | higher |
| OpenAI "Actions" / plugins | that vendor | network | vendor-locked |

### The version trap in these files

```python
# mcp 2.x renamed FastMCP to MCPServer, but langchain-mcp-adapters (the
# client side of this integration) requires mcp<2. Server and client have
# to agree, so this targets the 1.x API.
```

`mcp[cli]==1.30.0` is pinned, and `requirements.txt` says why. MCP is young and
moving; the client library lags the spec. Worth expecting when you adopt it.

## Run it

```bash
.venv/Scripts/python.exe learn/09-mcp-protocol/run.py
```

It launches the project's real currency and weather servers as subprocesses and
speaks JSON-RPC to them **by hand** — printing every byte in both directions.
Then it does the same thing through `langchain-mcp-adapters` for comparison,
demonstrates the degraded path by connecting to a server that does not exist,
and shows a tool failing honestly via the envelope.

## Next

[10 — The agent loop](../10-agent-loop/) — where it all comes together.
