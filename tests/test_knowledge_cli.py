import json
import sys

from rusa_knowledge import cli


class FakeKnowledgeService:
    def __init__(self):
        self.search_calls = []
        self.closed = False

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return [{
            "document_id": "document-1",
            "source_uri": "rusa://agent-knowledge/aicode-analyst/RUSA-AGENT-001",
        }]

    def close(self):
        self.closed = True


def test_evaluate_cli_passes_agent_and_project_scope(monkeypatch, tmp_path, capsys):
    dataset = tmp_path / "gold.jsonl"
    dataset.write_text(
        json.dumps({
            "query": "What are the analyst agent responsibilities?",
            "relevant_source_uris": [
                "rusa://agent-knowledge/aicode-analyst/RUSA-AGENT-001"
            ],
        }) + "\n",
        encoding="utf-8",
    )
    service = FakeKnowledgeService()
    monkeypatch.setattr(
        cli.KnowledgeService,
        "from_env",
        lambda *, check_ready=True: service,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rusa-knowledge",
            "evaluate",
            "--dataset", str(dataset),
            "--tenant-id", "tenant-a",
            "--collection", "agent",
            "--agent-id", "aicode-analyst",
            "--project-id", "strategyg--rusaai",
            "--k", "5",
        ],
    )

    assert cli.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["recall_at_k"] == 1.0
    assert report["scope"] == {
        "tenant_id": "tenant-a",
        "collection": "agent",
        "agent_id": "aicode-analyst",
        "project_id": "strategyg--rusaai",
    }
    assert service.search_calls == [{
        "tenant_id": "tenant-a",
        "collection": "agent",
        "query": "What are the analyst agent responsibilities?",
        "limit": 5,
        "agent_id": "aicode-analyst",
        "project_id": "strategyg--rusaai",
    }]
    assert service.closed is True


def test_evaluate_cli_keeps_shared_only_defaults(monkeypatch, tmp_path, capsys):
    dataset = tmp_path / "shared.jsonl"
    dataset.write_text(
        json.dumps({
            "query": "What is the RUSA platform?",
            "relevant_source_uris": ["rusa://knowledge-source/RUSA-ARCH-001"],
        }) + "\n",
        encoding="utf-8",
    )
    service = FakeKnowledgeService()
    monkeypatch.setattr(
        cli.KnowledgeService,
        "from_env",
        lambda *, check_ready=True: service,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rusa-knowledge",
            "evaluate",
            "--dataset", str(dataset),
            "--tenant-id", "tenant-a",
            "--collection", "agent",
        ],
    )

    assert cli.main() == 0
    assert service.search_calls[0]["collection"] == "agent"
    assert service.search_calls[0]["agent_id"] is None
    assert service.search_calls[0]["project_id"] is None
    assert service.closed is True
