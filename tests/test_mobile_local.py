from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mobile-local"))
pytest.importorskip("mcp")

from mcp_tools import _safe_calculate, call_local_tool, configure_store  # noqa: E402
from store import LocalStore  # noqa: E402


@pytest.fixture
def local_store(tmp_path):
    store = LocalStore(tmp_path / "hybrida.sqlite3")
    configure_store(store)
    return store


def test_ingest_search_and_read_local_document(local_store):
    result = local_store.ingest_text(
        title="Android handbook",
        source_uri="local-upload://android.md",
        collection="default",
        content="Termux can run llama.cpp on Android without root access. Keep model files in the Termux home directory.",
        metadata={"filename": "android.md"},
    )
    assert result["duplicate"] is False
    hits = local_store.search("run llama.cpp Android", collection="default", limit=3)
    assert hits
    assert hits[0]["document_id"] == result["document_id"]
    assert "Termux" in hits[0]["content"]
    document = local_store.get_document(result["document_id"])
    assert document is not None
    assert document["title"] == "Android handbook"


def test_duplicate_ingestion_is_idempotent(local_store):
    kwargs = {
        "title": "Notes",
        "source_uri": "local://notes.txt",
        "content": "Local retrieval stays on this device.",
    }
    first = local_store.ingest_text(**kwargs)
    second = local_store.ingest_text(**kwargs)
    assert first["document_id"] == second["document_id"]
    assert second["duplicate"] is True
    assert len(local_store.list_documents()) == 1


def test_conversation_persistence_and_delete(local_store):
    chat_id = local_store.create_conversation()
    local_store.append_message(chat_id, "user", "Halo HYBRIDA")
    local_store.append_message(chat_id, "assistant", "Halo, saya berjalan lokal.")
    messages = local_store.get_messages(chat_id)
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert local_store.list_conversations()[0]["title"] == "Halo HYBRIDA"
    assert local_store.delete_conversation(chat_id) is True
    assert local_store.get_messages(chat_id) == []


def test_mcp_tools_use_local_store(local_store):
    result = call_local_tool("create_note", {"title": "Ide", "content": "Buat aplikasi AI lokal."})
    assert result["saved"] is True
    found = call_local_tool("search_knowledge", {"query": "aplikasi AI lokal", "collection": "notes"})
    assert found["results"]
    assert call_local_tool("get_local_time", {})["local_time"]


def test_calculator_is_bounded_and_rejects_code():
    assert _safe_calculate("(125 * 4) / 5") == 100
    assert _safe_calculate("2 ** 3") == 8
    with pytest.raises(ValueError):
        _safe_calculate("__import__('os').system('id')")
    with pytest.raises(ValueError):
        _safe_calculate("2 ** 99")


def test_document_and_message_limits(local_store):
    with pytest.raises(ValueError):
        local_store.ingest_text(
            title="Too large", source_uri="local://large.txt", content="x" * 2_000_001
        )
    chat_id = local_store.create_conversation()
    with pytest.raises(ValueError):
        local_store.append_message(chat_id, "user", "x" * 30_001)
