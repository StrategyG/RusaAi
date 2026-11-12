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
