from rusa_knowledge.cli import build_parser


def test_ingest_cli_defaults_to_shared_knowledge_scope():
    args = build_parser().parse_args(
        ["ingest", "--file", "guide.md", "--tenant-id", "tenant-a"]
    )
    assert args.knowledge_scope == "shared"
    assert args.agent_id == []
    assert args.project_id is None


def test_ingest_cli_accepts_agent_and_project_scope():
    args = build_parser().parse_args(
        [
            "ingest",
            "--file",
            "analyst.md",
            "--tenant-id",
            "tenant-a",
            "--knowledge-scope",
            "agent_specific",
            "--agent-id",
            "aicode-analyst",
            "--agent-id",
            "aicode-security",
            "--project-id",
            "strategyg--rusaai",
        ]
    )
    assert args.knowledge_scope == "agent_specific"
    assert args.agent_id == ["aicode-analyst", "aicode-security"]
    assert args.project_id == "strategyg--rusaai"


def test_evaluation_cli_accepts_agent_and_project_scope():
    args = build_parser().parse_args(
        [
            "evaluate",
            "--dataset",
            "relevance.jsonl",
            "--tenant-id",
            "tenant-a",
            "--collection",
            "agent",
            "--agent-id",
            "aicode-analyst",
            "--project-id",
            "strategyg--rusaai",
        ]
    )
    assert args.agent_id == "aicode-analyst"
    assert args.project_id == "strategyg--rusaai"


def test_version_history_cli_requires_document_and_version_scope():
    parser = build_parser()
    versions = parser.parse_args(
        [
            "versions",
            "--tenant-id",
            "tenant-a",
            "--document-id",
            "00000000-0000-0000-0000-000000000001",
            "--agent-id",
            "aicode-analyst",
            "--project-id",
            "project-alpha",
        ]
    )
    assert versions.command == "versions"
    assert versions.collection == "agent"
    assert versions.limit == 20
    assert versions.agent_id == "aicode-analyst"
    assert versions.project_id == "project-alpha"

    version = parser.parse_args(
        [
            "get-version",
            "--tenant-id",
            "tenant-a",
            "--document-id",
            "00000000-0000-0000-0000-000000000001",
            "--version-id",
            "00000000-0000-0000-0000-000000000002",
        ]
    )
    assert version.command == "get-version"
    assert version.max_chars == 20_000
