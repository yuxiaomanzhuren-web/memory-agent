"""Shared tool schemas and dispatch for MCP, function calling, and HTTP."""
from __future__ import annotations

from copy import deepcopy
import logging

from .ingest import AgentRequestError, SCOPE_FIELDS, SOURCE_KINDS, unavailable

CONVERSATION = {
    "type": "object",
    "properties": {field: {"type": "string"} for field in SCOPE_FIELDS},
    "required": list(SCOPE_FIELDS),
    "additionalProperties": False,
}
OPERATIONS = frozenset(("remember", "recall", "judge", "add", "search", "get", "delete", "recent"))


def tool_definitions():
    query = {"type": "string", "minLength": 1, "maxLength": 1000}
    def definition(name, description, fields, required):
        return {
            "name": name, "description": description,
            "parameters": {
                "type": "object",
                "properties": {"conversation": deepcopy(CONVERSATION), **deepcopy(fields)},
                "required": ["conversation", *required],
                "additionalProperties": False,
            },
        }
    return [
        definition("remember", "Record an original message with its observed speaker and source.",
                   {"text": {"type": "string", "minLength": 1, "maxLength": 8000},
                    "speaker": {"type": "string", "pattern": r"^(user|assistant|other:.+)$"},
                    "at": {"type": "string", "format": "date-time"},
                    "source_kind": {"type": "string", "enum": sorted(SOURCE_KINDS)},
                    "message_id": {"type": "string", "minLength": 1, "maxLength": 128}},
                   ("text", "speaker", "at", "source_kind")),
        definition("recall", "Recall evidence with its verdict, gaps, and allowed_raw_ids.",
                   {"query": query, "limit": {"type": "integer", "minimum": 1, "maximum": 3}}, ("query",)),
        definition("judge", "Run the four-state verdict and record it in the judgment ledger.",
                   {"query": query}, ("query",)),
    ]


def openai_tools(api="chat_completions"):
    """Return the function definitions for the selected OpenAI API format."""
    definitions = tool_definitions()
    if api == "chat_completions":
        return [{"type": "function", "function": definition} for definition in definitions]
    if api == "responses":
        return [{"type": "function", "strict": False, **definition} for definition in definitions]
    raise ValueError("api must be chat_completions or responses")


def call_memory(memory, name, data):
    if name not in OPERATIONS:
        raise AgentRequestError("unknown_tool", 404)
    try:
        transport = getattr(memory, "call", None)
        if callable(transport):
            return transport(name, data)
        if name == "add":
            if not isinstance(data, dict) or set(data) != {"text", "metadata"} or not isinstance(data["metadata"], dict):
                raise AgentRequestError("invalid_add")
            result = memory.remember({**data["metadata"], "text": data["text"]})
            return {**result, "id": result["raw_id"]}
        if name == "search":
            name = "recall"
        return getattr(memory, name)(data)
    except AgentRequestError:
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning("memory-core operation %s failed (%s)", name, type(exc).__name__)
        raise unavailable() from None


def call_tool(memory, name, arguments):
    """Dispatch a parsed function call to memory-core and return a tool payload."""
    try:
        if name not in ("remember", "recall", "judge"):
            raise AgentRequestError("unknown_tool", 404)
        return call_memory(memory, name, arguments)
    except AgentRequestError as exc:
        return exc.payload()
