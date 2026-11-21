"""Exercise the installed protocol boundaries against a disposable archive."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
from typing import TypedDict
import unittest
from unittest.mock import patch

try:
    from langgraph.graph import START, END, StateGraph
    from langgraph.runtime import Runtime
    HAS_LANGGRAPH = True
except ImportError:  # 可选依赖没装：跳过而不是收集阶段就炸
    HAS_LANGGRAPH = False

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    HAS_MCP = HAS_LANGGRAPH and True
except ImportError:
    HAS_MCP = False

# 这不只是「测试环境缺依赖」：适配器本身就是靠这些包工作的，
# 缺了就该 skip，并且 README/pyproject 里写清要装哪一组可选依赖。
OPTIONAL_STACK = unittest.skipUnless(
    HAS_LANGGRAPH and HAS_MCP, "需要 memory-agent[langgraph] 与 memory-agent[mcp]（mcp 1.x）")

PACKAGE = os.environ.get("MEMORY_TEST_PACKAGE", "memory_core")
ingest = importlib.import_module(PACKAGE + ".adapters.ingest")
http = importlib.import_module(PACKAGE + ".adapters.http")
transport = importlib.import_module(PACKAGE + ".adapters.http.client")
tools = importlib.import_module(PACKAGE + ".adapters.tools")
store_module = importlib.import_module(PACKAGE + ".adapters.langgraph")
citation = importlib.import_module(PACKAGE + ".adapters.citation")
policy = importlib.import_module(PACKAGE + ".citation").CITATION_POLICY

CONVERSATION = {
    "source_id": "fixture-app", "owner_id": "fixture-person",
    "conversation_id": "0ad51bb8-b43a-4961-bf55-d424ef1a39fd",
}
QUOTE = "早餐喝了燕麦牛奶"
QUESTION = f"我说过『{QUOTE}』吗"
CASES = {
    "found": QUESTION,
    "suspected": f"我在2026-01-21说过『{QUOTE}』吗",
    "absent": "你还记得我上次养鲨鱼了吗？",
    "not_checked": "今天阳光挺好。",
}


def message(text=QUOTE, speaker="user", **kwargs):
    return {"text": text, "speaker": speaker, "source_kind": "direct",
            "conversation": CONVERSATION, "at": "2026-01-20T09:15:00+08:00", **kwargs}


class GraphState(TypedDict, total=False):
    seed: bool
    query: str
    result: dict


def compile_graph(store):
    def node(state: GraphState, runtime: Runtime) -> dict:
        if state.get("seed"):
            runtime.store.put(store.namespace, "graph-user", message())
            runtime.store.put(store.namespace, "graph-assistant", message("收到，我把这一条记下了", "assistant"))
        return {"result": runtime.store.search(store.namespace, query=state["query"]).as_dict()}
    graph = StateGraph(GraphState)
    graph.add_node("memory", node)
    graph.add_edge(START, "memory")
    graph.add_edge("memory", END)
    return graph.compile(store=store)


@OPTIONAL_STACK
class AdaptersTests(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get("MEMORY_TEST_TEMP", Path.cwd())).resolve()
        archive = tempfile.TemporaryDirectory(dir=parent, prefix="memory-adapters-")
        path = Path(archive.name).resolve()
        assert path.parent == parent
        self.addCleanup(archive.cleanup)
        self.memory = ingest.AgentMemory(path, CONVERSATION)
        self.addCleanup(self.memory.store.conn.close)
        self.token = secrets.token_hex(24)
        self.server = http.IngestServer(0, self.token, self.memory)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_http)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = transport.HTTPMemoryClient(self.url, self.token, CONVERSATION, timeout=15)
        self.backend = transport.MemoryBackend(self.client)
        self.store = store_module.MemoryStore(self.client)
        self.graph = compile_graph(self.store)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_AGENT_")}
        self.env.update({"MEMORY_AGENT_" + field.upper(): val for field, val in CONVERSATION.items()})
        self.env.update(MEMORY_AGENT_HTTP_URL=self.url, MEMORY_AGENT_HTTP_TOKEN=self.token,
                        MEMORY_AGENT_MCP_USE_HTTP="1", PYTHONUTF8="1")

    def stop_http(self):
        if self.worker.is_alive():
            self.server.shutdown()
            self.worker.join(5)
        self.server.server_close()

    def mcp(self):
        params = StdioServerParameters(
            command=sys.executable, args=["-B", "-m", PACKAGE + ".adapters.mcp_stdio"],
            env=self.env, cwd=str(Path.cwd()), encoding="utf-8",
        )
        return stdio_client(params)

    def hook(self, name, **fields):
        events = {"session-start": "SessionStart", "user-prompt": "UserPromptSubmit", "stop": "Stop"}
        payload = {"hook_event_name": events[name], "session_id": CONVERSATION["conversation_id"],
                   "cwd": str(Path.cwd()), "transcript_path": "fixture-transcript.jsonl", **fields}
        return subprocess.run(
            [sys.executable, "-B", "-m", PACKAGE + ".adapters.claude_code", f"--event={name}"],
            input=json.dumps(payload, ensure_ascii=False), text=True, encoding="utf-8",
            capture_output=True, env=self.env, timeout=12,
        )

    def assert_envelope(self, result, state, raw_ids):
        self.assertEqual((result["state"], result["allowed_raw_ids"]), (state, raw_ids))
        self.assertEqual(len(result["items"]), len(raw_ids))
        if state != "found":
            self.assertEqual(result["items"], [])

    def test_graph_http_mcp_and_tools_share_all_four_states(self):
        graph_reply = self.graph.invoke({"seed": True, "query": QUESTION})["result"]
        raw = self.store.get(self.store.namespace, "graph-user").value["id"]
        self.assert_envelope(graph_reply, "found", [raw])
        self.assertEqual(self.store.get(self.store.namespace, "graph-assistant").value["speaker"], "assistant")
        self.assertIsNone(self.store.get(self.store.namespace, "missing"))

        async def compare():
            async with self.mcp() as (receive, send), ClientSession(receive, send) as mcp:
                await mcp.initialize()
                response = await mcp.list_tools()
                definitions = tools.openai_tools()
                schemas = {d["function"]["name"]: d["function"]["parameters"] for d in definitions}
                self.assertEqual({tool.name: tool.inputSchema for tool in response.tools}, schemas)
                self.assertEqual({d["name"] for d in tools.openai_tools("responses")}, set(schemas))
                for expected, query in CASES.items():
                    request = {"conversation": CONVERSATION, "query": query}
                    raw_ids = [raw] if expected == "found" else []
                    actual = await mcp.call_tool("recall", request)
                    self.assertFalse(actual.isError, actual.structuredContent)
                    self.assert_envelope(actual.structuredContent, expected, raw_ids)
                    self.assertEqual(actual.structuredContent["citation_policy"], policy)
                    self.assert_envelope(self.client.call("recall", request), expected, raw_ids)
                    self.assert_envelope(self.backend.search(query), expected, raw_ids)
                    self.assert_envelope(tools.call_tool(self.memory, "recall", request), expected, raw_ids)
                    self.assert_envelope(tools.call_tool(self.client, "recall", request), expected, raw_ids)
                    found = await self.store.asearch(self.store.namespace, query=query)
                    self.assert_envelope(found.as_dict(), expected, raw_ids)
                    self.assertEqual(found.as_dict()["citation_policy"], policy)
                    self.assertEqual(found.state, expected)
                    if expected == "found":
                        self.assertEqual(found[0].key, "graph-user")
                        self.assertEqual(self.store.get(self.store.namespace, found[0].key).value["id"], raw)
                self.assert_envelope((await mcp.call_tool("judge", request)).structuredContent, "not_checked", [])

        asyncio.run(compare())
        scope = self.memory.pipeline.conn.execute("SELECT COUNT(*) FROM judgment_ledger").fetchone()[0]
        self.assertGreaterEqual(scope, len(CASES) * 6)

    def test_missing_speaker_consistent_and_namespace_bound(self):
        invalid = {k: v for k, v in message().items() if k != "speaker"}
        with self.assertRaises(ingest.AgentRequestError) as failure:
            self.client.call("remember", invalid)
        expected = {"error": "missing_speaker", "status": 400, "field": "speaker"}
        self.assertEqual(failure.exception.payload(), expected)
        with self.assertRaises(ValueError) as failure:
            self.store.put(self.store.namespace, "missing-speaker", invalid)
        self.assertEqual(failure.exception.payload(), expected)
        self.assertEqual(tools.call_tool(self.memory, "remember", invalid), expected)

        async def rejected():
            async with self.mcp() as (receive, send), ClientSession(receive, send) as mcp:
                await mcp.initialize()
                response = await mcp.call_tool("remember", invalid)
                self.assertTrue(response.isError)
                self.assertEqual(response.structuredContent, expected)
        asyncio.run(rejected())
        self.assertEqual(self.memory._originals(), {})
        with self.assertRaisesRegex(ValueError, "scope_forbidden"):
            self.store.search(("another-app",), query=QUESTION)
        with self.assertRaisesRegex(ValueError, "scope_forbidden"):
            self.backend.search(QUESTION, filters={"owner_id": "somebody-else"})
        with self.assertRaisesRegex(ValueError, "unsupported_filter"):
            self.backend.search(QUESTION, filters={"speaker": "user"})

    def test_hook_records_both_speakers_and_injects_authoritative_ids(self):
        seeded = self.backend.add(QUOTE, {k: v for k, v in message().items() if k != "text"})["id"]
        started = self.hook("session-start")
        self.assertEqual(started.returncode, 0, started.stderr)
        context = json.loads(started.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(json.loads(context.split("\n", 1)[1])["recent"][0]["raw_id"], seeded)
        submitted = self.hook("user-prompt", prompt=QUESTION)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        data = json.loads(submitted.stdout)["hookSpecificOutput"]
        self.assertEqual(data["hookEventName"], "UserPromptSubmit")
        self.assert_envelope(json.loads(data["additionalContext"].split("\n", 1)[1]), "found", [seeded])
        answer = "我查到那句早餐记录了"
        stopped = self.hook("stop", last_assistant_message=answer)
        self.assertEqual((stopped.returncode, stopped.stdout, stopped.stderr), (0, "", ""))
        rows = list(self.memory._originals().values())
        self.assertCountEqual([(r["r"], r["t"]) for r in rows if r["id"] != seeded],
                              [("user", QUESTION), ("assistant", answer)])
        crossed = self.hook("session-start", session_id="some-other-session")
        self.assertEqual((crossed.returncode, crossed.stdout), (0, ""))
        self.assertIn("hook_session_mismatch", crossed.stderr)

    def test_offline_backend_reports_unavailable_on_every_transport(self):
        request = {"conversation": CONVERSATION, "query": QUESTION}
        self.stop_http()
        with self.assertRaises(ingest.AgentRequestError) as unavailable:
            self.client.call("recall", request)
        self.assertEqual(unavailable.exception.payload(), ingest.unavailable().payload())
        self.assertEqual(tools.call_tool(self.client, "recall", request), ingest.unavailable().payload())
        with self.assertRaisesRegex(ValueError, "memory_unavailable"):
            self.store.search(self.store.namespace, query=QUESTION)

        async def blocked():
            async with self.mcp() as (receive, send), ClientSession(receive, send) as mcp:
                await mcp.initialize()
                result = await mcp.call_tool("recall", request)
                self.assertTrue(result.isError)
                self.assertEqual(result.structuredContent, ingest.unavailable().payload())
        asyncio.run(blocked())
        for event, fields in (("session-start", {}), ("user-prompt", {"prompt": QUESTION}),
                              ("stop", {"last_assistant_message": "收到"})):
            with self.subTest(event=event):
                failed = self.hook(event, **fields)
                self.assertEqual((failed.returncode, failed.stdout), (0, ""))
                self.assertIn("记忆不可用", failed.stderr)
                self.assertNotIn("absent", failed.stderr)

    def test_get_delete_and_audit_are_scoped_and_failure_rolls_back(self):
        value = message()
        self.store.put(self.store.namespace, "deletion", value)
        self.store.put(self.store.namespace, "deletion", value)
        identifier = self.store.get(self.store.namespace, "deletion").value["id"]
        self.assertEqual(self.backend.get(identifier)["text"], QUOTE)
        self.assertEqual(len(self.memory._originals()), 1)
        with patch.object(self.memory.pipeline, "_audit", side_effect=sqlite3.OperationalError("fixture audit failure")):
            with self.assertRaisesRegex(ValueError, "memory_unavailable"):
                self.backend.delete(identifier)
        self.assertEqual(self.backend.get(identifier)["text"], QUOTE)
        self.store.delete(self.store.namespace, "deletion")
        self.assertIsNone(self.store.get(self.store.namespace, "deletion"))
        self.assertIsNone(self.backend.get(identifier))
        self.assert_envelope(self.backend.search(QUESTION), "absent", [])
        self.backend.delete(identifier)
        detail, = self.memory.pipeline.conn.execute(
            "SELECT detail FROM memory_pipeline_audit WHERE operation='raw_soft_delete'").fetchall()
        entry = json.loads(detail[0])
        self.assertEqual(entry["subject"], {"type": "raw", "id": identifier})
        self.assertTrue(entry["raw_retained"])
        self.assertEqual(entry["scope"], self.memory.scope)
        with self.assertRaisesRegex(ValueError, "record_retired"):
            self.store.put(self.store.namespace, "deletion", value)

    def test_unavailable_judgment_ledger_never_returns_a_verdict(self):
        gates = importlib.import_module(PACKAGE + ".operation_ledger")
        with patch.object(gates, "record_gate", return_value={"status": "failed"}):
            result = tools.call_tool(self.memory, "judge", {"conversation": CONVERSATION, "query": QUESTION})
        self.assertEqual(result, ingest.unavailable().payload())

    def test_missing_assigned_raw_file_is_unavailable(self):
        raw_id = self.backend.add(QUOTE, {k: v for k, v in message().items() if k != "text"})["id"]
        record = self.memory._originals()[raw_id]
        (Path(self.memory.raw.root) / record["file"]).unlink()
        query = {"conversation": CONVERSATION, "query": QUESTION}
        for operation in ("judge", "recall", "search"):
            with self.subTest(operation=operation), self.assertRaises(ingest.AgentRequestError) as failure:
                self.client.call(operation, query)
            self.assertEqual(failure.exception.payload(), ingest.unavailable().payload())

    def test_installed_citation_endpoint_checks_actual_injection_then_blocks_on_shutdown(self):
        payload = message()
        payload["message_id"] = "citation-original"
        written = self.client.call("remember", payload)
        result = self.client.call("judge", {"conversation": CONVERSATION, "query": QUESTION})
        self.assertEqual(result["citation_policy"], policy)
        snapshot = citation.citation_snapshot(result, QUOTE)
        self.assertEqual(snapshot["allowed_raw_ids"], [written["raw_id"]])
        self.assertEqual(citation.citation_snapshot(result, "unused context")["allowed_raw_ids"], [])
        good = f"你之前说过「{QUOTE}」"
        bad = "你之前说过「我们明天要卖船」"
        self.assertTrue(self.store.guard(good, snapshot)["allowed"])
        self.assertEqual(tools.guard(bad, snapshot, client=self.client)["violations"][0]["reason"], "no_allowed_raw_match")
        self.stop_http()
        failed = self.store.guard(good, snapshot)
        self.assertFalse(failed["allowed"])
        self.assertEqual(failed["violations"][0]["reason"], "citation_check_unavailable")


if __name__ == "__main__":
    unittest.main()
