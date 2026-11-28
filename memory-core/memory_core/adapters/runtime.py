"""Configure and own one local memory archive for a transport process."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit

from ..memory_pipeline import run_pipeline
from ..source_provenance import configure_trace
from .ingest import AgentMemory, AgentRequestError, SCOPE_FIELDS, conversation_scope


@contextmanager
def archive_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "agent.lock").open("a+b", buffering=0) as lock_file:
        try:
            if os.name == "nt":
                import msvcrt
                # Byte-range locks can extend past EOF on Windows.
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AgentRequestError("archive_in_use", 409) from None
        yield
        # The OS releases the lock on close, including when the host exits.


def configured_model():
    url = os.environ.get("MEMORY_AGENT_LLM_URL")
    model = os.environ.get("MEMORY_AGENT_LLM_MODEL")
    key = os.environ.get("MEMORY_AGENT_LLM_KEY", "")
    if not any((url, model, key)):
        return None
    if not url or not model:
        raise AgentRequestError("incomplete_provider_config")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
        raise AgentRequestError("invalid_provider_url")

    def complete(prompt, system, max_tokens):
        import httpx
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            response = httpx.post(
                url, headers=headers, timeout=30, follow_redirects=False,
                json={"model": model, "max_tokens": max_tokens, "temperature": 0.3,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]},
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty response")
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
            raise RuntimeError("extraction_provider_unavailable") from None
        return text

    return complete


@contextmanager
def open_memory():
    conversation = {field: os.environ.get("MEMORY_AGENT_" + field.upper()) for field in SCOPE_FIELDS}
    conversation_scope(conversation)
    data_dir = os.environ.get("MEMORY_AGENT_DATA_DIR")
    if not data_dir:
        raise AgentRequestError("data_directory_required")
    directory = Path(data_dir).expanduser().resolve()
    from ..isolated_scope import is_managed_area

    if is_managed_area(directory):
        raise AgentRequestError("managed_archive_requires_isolated_serve", 409)
    model = configured_model()
    with archive_lock(directory):
        configure_trace(directory / "source-provenance.jsonl")
        memory = AgentMemory(directory, conversation, model=model)
        stop = threading.Event()
        worker = None
        if model:
            worker = threading.Thread(target=run_pipeline, args=(memory.pipeline, stop), daemon=True)
            worker.start()
        try:
            yield memory
        finally:
            stop.set()
            if worker:
                worker.join()
            memory.store.conn.close()
