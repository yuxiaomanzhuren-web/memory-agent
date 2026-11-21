"""MCP tools backed by a conversation-bound memory instance."""
from __future__ import annotations

from contextlib import nullcontext, redirect_stdout
import json
import os
import sys

import anyio

try:
    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
except ImportError as exc:  # 没装可选依赖，或者装成了不兼容的版本
    raise ImportError(
        "MCP 适配器需要 `mcp` 1.x：pip install \"memory-agent[mcp]\""
    ) from exc

if not hasattr(Server, "list_tools"):
    # mcp 2.x 去掉了 Server.list_tools()/call_tool() 这套注册方式。
    # 与其在运行到一半时抛 AttributeError，不如在这里把原因说清楚。
    raise ImportError(
        "检测到的 `mcp` 版本没有 Server.list_tools()（多半是 2.x）。"
        "本仓目前只适配 1.x：pip install \"mcp>=1,<2\""
    )

from ..ingest import AgentRequestError
from ..http.client import HTTPMemoryClient
from ..runtime import open_memory
from ..tools import call_tool as dispatch_tool, tool_definitions


def make_server(memory):
    server = Server("memory-agent")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=tool["name"], description=tool["description"],
                           inputSchema=tool["parameters"]) for tool in tool_definitions()]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        data = await anyio.to_thread.run_sync(dispatch_tool, memory, name, arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
            structuredContent=data, isError="error" in data,
        )

    return server


async def serve():
    # Capture the SDK's wire streams first, then keep core diagnostics off stdout.
    async with stdio_server() as (incoming, outgoing):
        backend = (nullcontext(HTTPMemoryClient.from_env())
                   if os.getenv("MEMORY_AGENT_MCP_USE_HTTP") == "1" else open_memory())
        with redirect_stdout(sys.stderr), backend as memory:
            server = make_server(memory)
            await server.run(incoming, outgoing, server.create_initialization_options())


def main():
    try:
        anyio.run(serve)
    except (AgentRequestError, OSError) as exc:
        reason = exc.code if isinstance(exc, AgentRequestError) else type(exc).__name__
        print(f"[memory-agent] startup refused: {reason}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
