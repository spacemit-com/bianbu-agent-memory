"""MCP stdio server exposing AgentMemory as bianbu.memory tools.

The Runtime loads this server from the bianbu.memory plugin
(``mcps/memory/mcp.json``) and namespaces every tool as
``bianbu.memory/memory/<tool>``.  Tool bodies live in :mod:`.tools` and
:mod:`.sessions`; this module only owns the MCP surface: schemas, annotations,
host context extraction and the Worker-attested identity lifetime.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from . import sessions as session_tools
from . import tools as memory_tools
from .context import HostContext, WorkerRegistration, host_context

LOGGER = logging.getLogger("bianbu_agent_sdk.memory.mcp_server")

SERVER_NAME = "bianbu.memory"
TRANSPORT_ENV = "BIANBU_MEMORY_MCP_TRANSPORT"

MEMORY_DESCRIPTION = """\
Save durable AgentMemory facts that survive across sessions. Entries reach context \
only when the Agent selects the bianbu.memory Hooks, so keep them compact and \
high-signal.

BACKEND: AgentMemory accepts exactly one operation per call. Pass the single-op
shape (target + action), or an operations array holding exactly one operation.

WHEN: save proactively when the user states a preference, correction, or individual
detail, or you learn a stable fact about their environment, conventions, or workflow.
Priority: user preferences & corrections > environment facts > procedures. The best
memory stops the user repeating themselves.

REMOVE: Runtime pauses before a remove call and displays an approval card. After the
user approves that card the same call executes AgentMemory Forget and returns its
deletion receipt. A chat message saying "确认删除" is not a substitute for approving
the card.

TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your
notes (environment, conventions, tool quirks, lessons). Use the target returned by
memory_retrieve; do not guess.

RESPONSE GATE: a Tool status of success only means the call returned. Claim that a
memory was recorded, revised, or removed only when the JSON result has success=true.
Never announce completion before reading the Tool result.

LOCATORS: prefer memory_id for an exact record. For single-valued preferences use the
returned slot; AgentMemory revises the newest record and reconciles same-slot
duplicates. Text matching is a compatibility fallback.

SKIP: trivial or obvious info, easily re-discovered facts, raw data dumps, task
progress, completed-work logs, and temporary TODO state (use session_search for
those). Reusable procedures belong in a skill, not memory.
"""

MEMORY_RECORD_DESCRIPTION = """\
Append one project-local note to AgentMemory.

Use this only for a note that is worth keeping across sessions and that does not
belong to the user profile; prefer the memory Tool when you know the target. The
call returns plain text, not JSON: say the memory was recorded only when the result
is exactly "memory recorded".
"""

MEMORY_RETRIEVE_DESCRIPTION = """\
Read bounded structured entries from AgentMemory.

Results contain memory_id, kind, target, slot, revision, source, score, text, and the
record timestamps. Use memory_id for an exact replace/remove, or slot for a correction
to a single-valued preference. Do not infer target from prose and do not claim a
mutation succeeded until the memory Tool returns JSON success=true.

BOUNDED READ: this Tool returns a capped window of the store, never the whole store.
`count` is how many entries this answer carries; `has_more` says whether the store
holds more beyond them. A window that is exactly as long as the cap and a complete
store look identical without it, so never present the entries as "all", "everything",
"the complete list", or the final word on what is remembered. When the user asks for
all memories, report what this read returned and, when `has_more` is true, say plainly
that more entries exist than the ones shown.

SCOPED READ: a non-empty `query` narrows the window to the entries that match it, so
what comes back is a selection from the store, not the store itself. `has_more` only
reports whether a qualifying entry was cut from the window, so it stays false when the
query is what left the other entries out. Never answer "what do you remember", "show me
the memories", or any other request for the whole memory from a query-scoped read: call
this Tool again with no `query` for that shape, and say "this is all I have on <topic>"
rather than "this is all I remember".

TIMESTAMPS: `updated_at` is bare epoch milliseconds, kept for machine use only, and
`updated_at_display` is the same instant already written the way a person reads a
clock, for example "2026-09-24 11:31".

Show a time to the user by copying `updated_at_display` verbatim. Do not restyle it and
do not quote the bare `updated_at` integer: a reformatted or raw timestamp reaches the
user as a different time than the one that was recorded.

TAGS: every entry carries its stored `tags`. The reserved tag `important` is the
mark the user asked for, so read it from there instead of guessing from the text;
`slot:` tags name the single-valued field the entry fills. Entries written by
automatic extraction also carry `mem0-derived`.

An empty answer is code no_match. If AgentMemory refused the read the result is
success=false with the backend code, for example unauthenticated; that is not an
empty memory, so say the lookup failed instead of saying nothing is remembered.
"""

SESSION_SEARCH_DESCRIPTION = """\
Search, read, and browse this device's past agent sessions.

Reads the AgentMemory session records written by the bianbu.memory plugin: each
completed run stores its user request and assistant reply, and in-progress or
interrupted runs leave a request watermark. No LLM calls are involved; every shape
returns recorded text.

SOURCE-FIRST LIMIT

  This tool only shows what was said in past sessions on this device. It is not
  evidence about the current contents of external sources. If the user provided a
  direct source such as a URL, contact, app/thread, file path, account, website, or
  live system, inspect that original source before or instead of session_search when
  accessible. Use session_search as context for what was previously said, not as
  proof of what a source currently contains. Do not conclude 'not found' or 'no prior
  correspondence' from session_search alone when a direct source was provided.

THREE CALLING SHAPES

  1) DISCOVERY - pass query:
     session_search(query="auth refactor", limit=3)
     Case-insensitive substring match over the recorded messages of other sessions.
     Returns up to limit matching messages, each with its session_id, title, and last
     activity time; sessions are visited newest first (pass sort="oldest" to visit
     oldest first). When the query asks for the beginning of history (第一个会话 /
     first session / earliest session), the oldest sessions that have readable
     transcripts are returned whole, with their full messages. Search covers the 20
   most recently active other sessions. Entries that carry `last_active_at` (bare
   epoch milliseconds, for machine use only) also carry `last_active_at_display` (the
   same instant written as "2026-09-24 11:31"). Show times by copying the display
   value verbatim, never the raw integer and never a restyled variant.

  2) READ - pass session_id:
     session_search(session_id="...")
     Returns the full recorded transcript of one session (user requests and assistant
     replies, oldest first). Use a session_id from a discovery result or a browse
     call. Takes precedence over query when both are given.

  3) BROWSE - no arguments:
     session_search()
     Returns up to limit recent sessions as one-line entries (session_id, title, last
     activity time, completed turns, and in-progress/interrupted flags), newest
     activity first (pass sort="oldest" for oldest first). The session running this
     call is excluded.

WHEN TO USE

  Reach for this on questions about past conversations, such as "what did we do
  about X", "where did we leave Y", or "find the session where Z". This tool shows
  what was said when; external tools show current source or world state.
"""


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Hold the Worker-attested memoryd identity for the process lifetime."""

    registration = WorkerRegistration()
    try:
        registration.open()
    except RuntimeError as exc:
        # Fail closed on the memoryd call instead of refusing to start: an
        # unmanaged development run has no broker, and the failed call is
        # reported to the model as AgentMemory being unavailable.
        LOGGER.warning("Worker memory registration failed: %s", exc)
    try:
        yield
    finally:
        registration.close()


def _host(ctx: Context | None) -> HostContext:
    request_context = getattr(ctx, "request_context", None)
    if request_context is None:
        return HostContext()
    return host_context(getattr(request_context, "meta", None))


def create_server() -> FastMCP:
    from mcp.types import ToolAnnotations

    server = FastMCP(SERVER_NAME, lifespan=_lifespan)

    @server.tool(
        name="memory",
        title="AgentMemory",
        description=MEMORY_DESCRIPTION,
        annotations=ToolAnnotations(
            title="AgentMemory",
            readOnlyHint=False,
            # The Runtime gates the destructive ``remove`` action itself, so the
            # server must not claim every call needs approval: add/replace save
            # memories the user asked for.
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def memory(
        ctx: Context,
        target: Annotated[
            str,
            Field(
                description=(
                    "Which memory store: 'user' for profile/preferences, 'memory' "
                    "for facts/decisions/project notes. Copy this from "
                    "memory_retrieve."
                )
            ),
        ],
        action: Annotated[
            str | None,
            Field(
                description=(
                    "The action to perform: add, replace, remove, mark_important, "
                    "or unmark_important. The mark actions toggle the "
                    "user-designated important mark on an existing record and do "
                    "not widen what the account uploads by themselves."
                )
            ),
        ] = None,
        content: Annotated[
            str | None,
            Field(description="The entry content. Required for add and replace."),
        ] = None,
        old_text: Annotated[
            str | None,
            Field(
                description=(
                    "A short substring identifying an existing entry. If it is "
                    "ambiguous, the result returns candidate memory IDs."
                )
            ),
        ] = None,
        memory_id: Annotated[
            str | None,
            Field(
                description=(
                    "Exact AgentMemory record ID for replace or remove. Prefer this "
                    "after an ambiguous old_text result."
                )
            ),
        ] = None,
        slot: Annotated[
            str | None,
            Field(
                description=(
                    "Stable single-value field returned by memory_retrieve, such as "
                    "appearance.terminal_color_scheme. Prefer this for preference "
                    "corrections. Passing slot asserts that content is that field's "
                    "value (for example dark, uv, Asia/Shanghai); store a sentence "
                    "without slot instead, because a declared field only accepts "
                    "its documented value."
                )
            ),
        ] = None,
        operations: Annotated[
            list[Any] | None,
            Field(
                description=(
                    "Batch shape; AgentMemory accepts exactly one operation per call."
                )
            ),
        ] = None,
    ) -> str:
        return await memory_tools.memory(
            _host(ctx),
            target=target,
            action=action,
            content=content,
            old_text=old_text,
            memory_id=memory_id,
            slot=slot,
            operations=operations,
        )

    @server.tool(
        name="memory_record",
        title="Record Memory",
        description=MEMORY_RECORD_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Record Memory",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def memory_record(
        ctx: Context,
        content: Annotated[str, Field(description="The content value.")],
    ) -> str:
        return await memory_tools.memory_record(_host(ctx), content=content)

    @server.tool(
        name="memory_retrieve",
        title="Retrieve Memory",
        description=MEMORY_RETRIEVE_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Retrieve Memory",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def memory_retrieve(
        ctx: Context,
        query: Annotated[
            str | None,
            Field(
                description=(
                    "Discovery shape. Words matched against remembered text: a "
                    "non-empty query narrows the read to the entries that match it, so "
                    "the answer is a selection rather than the whole memory. Omit the "
                    "query to list the most recent entries, which is the shape for "
                    "'what do you remember' and 'show me the memories'."
                )
            ),
        ] = None,
    ) -> str:
        return await memory_tools.memory_retrieve(_host(ctx), query=query)

    @server.tool(
        name="session_search",
        title="Session Search",
        description=SESSION_SEARCH_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Session Search",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def session_search(
        ctx: Context,
        query: Annotated[
            str | None,
            Field(
                description=(
                    "Discovery shape. Substring to find in other sessions' recorded "
                    "messages, case-insensitive. Omit and pass session_id to read one "
                    "session, or omit both to browse recent sessions."
                )
            ),
        ] = None,
        session_id: Annotated[
            str | None,
            Field(
                description=(
                    "Read shape. Session id from a discovery or browse result. Takes "
                    "precedence over query."
                )
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                description=(
                    "Max results to return (default 3, max 10). Discovery: matching "
                    "messages, or sessions for origin queries. Browse: sessions "
                    "listed. Ignored by the read shape."
                )
            ),
        ] = 3,
        sort: Annotated[
            str | None,
            Field(
                description=(
                    'Optional ordering. Set "oldest" to visit or list oldest sessions '
                    "first (discovery and browse shapes); omit for newest first."
                )
            ),
        ] = None,
    ) -> str:
        return await session_tools.session_search(
            _host(ctx),
            query=query,
            session_id=session_id,
            limit=limit,
            sort=sort,
        )

    return server


def main() -> None:
    create_server().run(transport=os.environ.get(TRANSPORT_ENV, "stdio"))


if __name__ == "__main__":
    main()
