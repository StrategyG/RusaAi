from pathlib import Path


def test_compose_does_not_share_all_secrets_with_every_service():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "env_file:" not in compose

    api_block = compose.split("  rusa-api:", 1)[1].split("  rusa-agent-worker:", 1)[0]
    worker_block = compose.split("  rusa-agent-worker:", 1)[1].split("\nvolumes:", 1)[0]

    assert "RUSA_API_KEY:" in api_block
    assert "RUSA_API_KEY:" not in worker_block
    assert "RUSA_GITHUB_TOKEN:" not in api_block
    assert "RUSA_PROVIDER_API_KEY:" not in api_block
    assert "RUSA_GITHUB_TOKEN:" in worker_block
    assert "RUSA_PROVIDER_API_KEY:" in worker_block
    assert "RUSA_PROVIDER_ALLOWED_HOSTS:" in worker_block
