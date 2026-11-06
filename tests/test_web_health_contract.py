"""Nginx liveness and readiness contract tests."""


from pathlib import Path


def test_nginx_liveness_is_unique_and_independent_of_upstream():
    config = (Path(__file__).resolve().parents[1] / "apps" / "web" / "nginx.conf").read_text(
        encoding="utf-8"
    )
    assert config.count("location = /health/live") == 1
    assert "location = /health/ready" in config

    start = config.index("location = /health/live")
    end = config.index("\n    }", start)
    liveness = config[start:end]
    assert "return 200" in liveness
    assert "proxy_pass" not in liveness
