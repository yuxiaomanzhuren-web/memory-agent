"""MCP tools backed by a conversation-bound memory instance."""
from __future__ import annotations

from contextlib import redirect_stdout
import json
import sys

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from ..ingest import AgentRequestError, SOURCE_KINDS
from ..runtime import open_memory


CONVERSATION = {
    "type": "object",
    "properties": {name: {"type": "string"} for name in
                   ("source_id", "owner_id", "conversation_id")},
    "required": ["source_id", "owner_id", "conversation_id"],
    "additionalProperties": False,
}


def tool(name, description, properties, required):
    return types.Tool(
        name=name, description=description,
        inputSchema={
            "type": "object",
            "properties": {"conversation": CONVERSATION, **properties},
            "required": ["conversation", *required],
            "additionalProperties": False,
        },
    )


def make_server(memory):
    server = Server("memory-agent")

    @server.list_tools()
    async def list_tools():
        return [
            tool("remember",
                 "Record an original message with its observed speaker and source. Use a stable message_id for retries. Extraction runs asynchronously.",
                 {"text": {"type": "string", "minLength": 1, "maxLength": 8000},
                  "speaker": {"type": "string", "pattern": r"^(user|assistant|other:.+)$"},
                  "at": {"type": "string", "format": "date-time"},
                  "source_kind": {"type": "string", "enum": sorted(SOURCE_KINDS)},
                  "message_id": {"type": "string", "minLength": 1, "maxLength": 128}},
                 ["text", "speaker", "at", "source_kind"]),
            tool("recall", "Return a verdict, evidence gaps, extracted memories and matching utterances with speaker labels.",
                 {"query": {"type": "string", "minLength": 1, "maxLength": 1000},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 3}},
                 ["query"]),
            tool("judge", "Run Gate 1 and record its four-state verdict in the judgment ledger.",
                 {"query": {"type": "string", "minLength": 1, "maxLength": 1000}},
                 ["query"]),
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            if name not in ("remember", "recall", "judge"):
                raise AgentRequestError("unknown_tool", 404)
            data = await anyio.to_thread.run_sync(getattr(memory, name), arguments)
        except AgentRequestError as exc:
            data = exc.payload()
            failed = True
        except Exception as exc:
            print(f"[memory-agent] tool failure: {type(exc).__name__}", file=sys.stderr, flush=True)
            data = AgentRequestError("memory_unavailable", 503).payload()
            failed = True
        else:
            failed = False
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
            structuredContent=data, isError=failed,
        )

    return server


async def serve():
    # Capture the SDK's wire streams first, then keep core diagnostics off stdout.
    async with stdio_server() as (incoming, outgoing):
        with redirect_stdout(sys.stderr), open_memory() as memory:
            server = make_server(memory)
            await server.run(incoming, outgoing, server.create_initialization_options())


def main():
    try:
        anyio.run(serve)
    except (AgentRequestError, OSError) as exc:
        reason = exc.code if isinstance(exc, AgentRequestError) else type(exc).__name__
        print(f"[memory-agent] startup refused: {reason}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
