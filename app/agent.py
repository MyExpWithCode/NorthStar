"""The agent: tool loop, conversation memory, and provenance extraction.

Tool selection is the model's job, driven by the tool descriptions and the
system prompt. There is deliberately no hand-written intent classifier: the
brief asks for appropriate tool selection based on user intent, and a keyword
router would be both worse at it and unable to handle the flagship scenario,
where the *result* of the weather call determines whether a second
knowledge-base search for indoor alternatives is needed.

Conversation memory is a LangGraph checkpointer keyed by `thread_id` -- the
session id -- so tool results stay in context and the model can refer back to a
forecast it already fetched instead of re-fetching it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ClearToolUsesEdit,
    ContextEditingMiddleware,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tracers.context import collect_runs
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app import llm, mcp_client, observability, prompts
from app.config import settings
from app.mcp_client import McpToolset, parse_tool_payload
from app.rag import retriever
from app.rag.kb_tool import search_travel_knowledge_base

logger = logging.getLogger(__name__)

KB_TOOL_NAME = search_travel_knowledge_base.name


@dataclass
class TravelAgent:
    """An assembled agent plus the toolset it was built from."""

    graph: Any
    toolset: McpToolset
    checkpointer: BaseCheckpointSaver
    model_name: str
    destinations: list[str] = field(default_factory=list)
    kb_tool_available: bool = True

    @property
    def tool_names(self) -> list[str]:
        return [KB_TOOL_NAME, *self.toolset.tool_names]

    def describe(self) -> dict:
        return {
            "model": self.model_name,
            "destinations": self.destinations,
            "tools": self.tool_names,
            "tools_by_server": {
                "knowledge_base": [KB_TOOL_NAME],
                **self.toolset.tools_by_server,
            },
            "degraded_tools": self.toolset.degraded_summary(),
            "tracing": observability.describe(),
        }


async def build_agent(
    checkpointer: BaseCheckpointSaver | None = None,
) -> TravelAgent:
    """Connect the MCP servers and assemble the agent.

    Called once at application startup. A failed MCP server reduces the tool
    list rather than preventing startup -- see `app.mcp_client`.

    `checkpointer` holds conversation history. The API passes a SQLite-backed
    one so conversations survive a restart; omitting it falls back to memory,
    which is what the smoke scripts want.
    """
    toolset = await mcp_client.connect()
    logger.info("MCP tools:\n%s", mcp_client.describe(toolset))

    covered = retriever.destination_names()
    logger.info("Knowledge base covers: %s", covered or "(nothing)")

    model = llm.get_chat_model()
    tools = [search_travel_knowledge_base, *toolset.tools]
    checkpointer = checkpointer or InMemorySaver()

    # Retrieval excerpts accumulate in history and are the bulk of every
    # request. Left alone, the third turn of a conversation gets rejected as
    # too large. This clears tool results from earlier turns once the context
    # passes the trigger, while keeping the most recent ones so the current
    # turn always still has its evidence.
    context_editing = ContextEditingMiddleware(
        edits=[
            ClearToolUsesEdit(
                trigger=settings.context_trim_trigger_tokens,
                keep=settings.context_trim_keep_results,
                clear_tool_inputs=True,
                placeholder=(
                    "[earlier tool result cleared to save context; "
                    "search again if you need it]"
                ),
            )
        ]
    )

    graph = create_agent(
        model,
        tools=tools,
        system_prompt=prompts.system_prompt(
            toolset.prompt_note(), destinations=covered
        ),
        checkpointer=checkpointer,
        middleware=[context_editing],
    )
    return TravelAgent(
        graph=graph,
        toolset=toolset,
        checkpointer=checkpointer,
        model_name=llm.active_model_name(),
        destinations=covered,
    )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------
@dataclass
class Provenance:
    """What the answer was built from, for the UI's evidence panels."""

    kb_sources: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    #: LangSmith URL for this turn's run, when tracing is on. Lets a reviewer
    #: inspect the whole agent loop rather than taking the panels on trust.
    trace_url: str | None = None

    def as_dict(self) -> dict:
        return {
            "kb_sources": self.kb_sources,
            "tool_calls": self.tool_calls,
            "trace_url": self.trace_url,
        }


def _tool_call_arguments(messages: list, tool_call_id: str) -> dict:
    """Find the arguments the model passed for a given tool result."""
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            if call.get("id") == tool_call_id:
                return call.get("args") or {}
    return {}


def extract_provenance(messages: list, toolset: McpToolset | None = None) -> Provenance:
    """Walk a run's messages and collect what each tool actually returned.

    Knowledge-base citations come from the tool's artifact rather than by
    parsing the answer text, so a citation cannot be invented by the model: if
    `[S1]` appears in the answer, a real retrieved chunk produced it.
    """
    provenance = Provenance()
    seen_chunks: set[str] = set()

    for message in messages:
        if not isinstance(message, ToolMessage):
            continue

        name = message.name or ""
        arguments = _tool_call_arguments(messages, message.tool_call_id)

        if name == KB_TOOL_NAME:
            artifact = message.artifact if isinstance(message.artifact, dict) else {}
            sources = artifact.get("sources") or []
            for source in sources:
                # The same chunk can be retrieved by two different searches in
                # one turn; cite it once.
                if source.get("chunk_id") in seen_chunks:
                    continue
                seen_chunks.add(source.get("chunk_id"))
                provenance.kb_sources.append(source)
            provenance.tool_calls.append(
                {
                    "server": "knowledge_base",
                    "tool": name,
                    "args": arguments,
                    "ok": bool(sources),
                    "detail": (
                        f"{len(sources)} excerpt(s)"
                        if sources
                        else "no content above the relevance floor"
                    ),
                    "retrieved_at": None,
                }
            )
            continue

        # An MCP tool result. Read our envelope for the honest status.
        payload = parse_tool_payload(message)
        ok = bool(payload.get("ok")) if payload else message.status != "error"
        entry = {
            "server": toolset.server_of(name) if toolset else None,
            "tool": name,
            "args": arguments,
            "ok": ok,
            "source": payload.get("source"),
            "retrieved_at": payload.get("retrieved_at"),
        }
        if not ok:
            entry["error"] = payload.get("error") or str(message.content)[:300]
        else:
            entry["detail"] = _summarise(name, payload.get("data") or {})
        provenance.tool_calls.append(entry)

    return provenance


def _summarise(tool_name: str, data: dict) -> str:
    """A one-line summary of a successful tool result, for the UI panel."""
    if tool_name == "get_weather_forecast":
        days = data.get("forecast") or []
        poor = sum(1 for d in days if d.get("outdoor_suitability") == "poor")
        return (
            f"{data.get('days_returned', len(days))} day(s) for "
            f"{data.get('location', 'destination')}"
            + (f", {poor} poor for outdoor activity" if poor else "")
        )
    if tool_name == "get_current_weather":
        return (
            f"{data.get('temperature_c')}°C, {data.get('conditions')} "
            f"in {data.get('location', '')}".strip()
        )
    if tool_name == "convert_currency":
        return (
            f"{data.get('amount')} {data.get('from_currency')} = "
            f"{data.get('converted_amount')} {data.get('to_currency')} "
            f"(rate published {data.get('rate_date')})"
        )
    if tool_name == "get_exchange_rate":
        return (
            f"1 {data.get('from_currency')} = {data.get('rate')} "
            f"{data.get('to_currency')} (published {data.get('rate_date')})"
        )
    return "ok"


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------
def _answer_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    # Some providers return content as a list of blocks.
    parts = []
    for block in content or []:
        if isinstance(block, dict):
            parts.append(block.get("text", ""))
        else:
            parts.append(str(block))
    return "".join(parts)


def _failure_message(exc: Exception) -> str:
    """Turn a provider exception into something a user can act on.

    Rate limits get their own wording because they are transient and
    common on metered free tiers, and because the raw provider payload is
    a wall of JSON. Either way the message says plainly that no travel
    information was produced, so a failure can never read as an answer.
    """
    name = type(exc).__name__
    text = str(exc)
    if "RateLimit" in name or "429" in text or "rate limit" in text.lower():
        return (
            "The language model is rate-limited right now, so I could not "
            "complete that request. No travel information was retrieved. "
            "Please try again in a minute."
        )
    if "413" in text or "too large" in text.lower():
        return (
            "That request was too large for the model provider to accept, "
            "so I could not complete it. No travel information was "
            "retrieved. Try asking for a shorter plan, or start a new "
            "conversation to shrink the history."
        )
    return (
        "I could not complete that request: " + name + ": " + text[:300]
        + "\n\nNothing above is a travel fact -- please try rephrasing, "
        + "or try again in a moment."
    )


async def ask(
    agent: TravelAgent, session_id: str, question: str
) -> tuple[str, Provenance]:
    """Run one turn. Returns the answer markdown and its provenance."""
    config = {"configurable": {"thread_id": session_id}}
    traced_run = None
    try:
        # collect_runs captures the root run so the answer can carry a link to
        # its own trace. It is a no-op when tracing is off.
        with collect_runs() as collected:
            result = await agent.graph.ainvoke(
                {"messages": [HumanMessage(content=question)]}, config=config
            )
        traced_run = collected.traced_runs[0] if collected.traced_runs else None
    except Exception as exc:
        # A chat turn must not die on a provider-side error. Report what
        # happened; never dress a failure up as an answer.
        logger.exception("Turn failed for session %s", session_id)
        return _failure_message(exc), Provenance()

    messages = result["messages"]

    # Only this turn's messages are new; provenance should not re-report tools
    # called in earlier turns.
    turn_messages = _messages_since_last_human(messages)
    provenance = extract_provenance(turn_messages, agent.toolset)
    provenance.trace_url = observability.run_url(traced_run)

    final = next(
        (m for m in reversed(messages) if isinstance(m, AIMessage) and _answer_text(m)),
        None,
    )
    return (_answer_text(final) if final else ""), provenance


def _messages_since_last_human(messages: list) -> list:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return messages[index:]
    return messages


def history(agent: TravelAgent, session_id: str) -> list[dict]:
    """The stored conversation for a session, for debugging and the UI."""
    config = {"configurable": {"thread_id": session_id}}
    state = agent.graph.get_state(config)
    messages = (state.values or {}).get("messages", []) if state else []
    return [
        {"role": message.type, "content": _answer_text(message)}
        for message in messages
        if isinstance(message, (HumanMessage, AIMessage)) and _answer_text(message)
    ]


async def list_conversations(agent: TravelAgent, limit: int = 40) -> list[dict]:
    """Recent conversations, newest first.

    The checkpointer stores several checkpoints per turn, so the scan groups by
    thread and keeps the most recent per thread. The first user message doubles
    as the title -- which is what someone scanning a list actually recognises.
    """
    saver = agent.checkpointer
    lister = getattr(saver, "alist", None)
    if lister is None:
        return []

    threads: dict[str, dict] = {}
    try:
        # Scan more checkpoints than conversations wanted, since each turn
        # writes several.
        async for tup in lister(None, limit=limit * 12):
            thread_id = (tup.config or {}).get("configurable", {}).get("thread_id")
            if not thread_id:
                continue
            messages = (tup.checkpoint or {}).get(
                "channel_values", {}
            ).get("messages") or []
            human = next(
                (m for m in messages if isinstance(m, HumanMessage)), None
            )
            entry = threads.get(thread_id)
            turns = sum(1 for m in messages if isinstance(m, HumanMessage))
            candidate = {
                "session_id": thread_id,
                "title": _answer_text(human)[:110] if human else "(no messages)",
                "turns": turns,
                "messages": len(messages),
                "updated_at": (tup.checkpoint or {}).get("ts"),
            }
            # alist yields newest first, so the first sighting of a thread is
            # its latest state.
            if entry is None or (candidate["messages"] or 0) > (entry["messages"] or 0):
                threads[thread_id] = candidate
    except Exception as exc:
        logger.warning("Could not list conversations: %s", exc)
        return []

    ordered = sorted(
        threads.values(), key=lambda c: c["updated_at"] or "", reverse=True
    )
    return ordered[:limit]


async def reset(agent: TravelAgent, session_id: str) -> None:
    """Forget one conversation, from the store as well as from memory."""
    saver = agent.checkpointer
    for name in ("adelete_thread", "delete_thread"):
        deleter = getattr(saver, name, None)
        if deleter is None:
            continue
        try:
            result = deleter(session_id)
            if name.startswith("a"):
                await result
            return
        except (NotImplementedError, AttributeError):
            continue
    logger.info(
        "Checkpointer cannot delete threads; %s left in place", session_id
    )
