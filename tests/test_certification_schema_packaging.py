from pathlib import Path


def test_packaged_certification_schemas_match_repository_contracts() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    source_dir = repository_root / "schemas" / "certification"
    package_dir = repository_root / "rusa_agents" / "certification" / "schemas"

    for filename in ("release-manifest.schema.json", "production-certification.schema.json"):
        assert (package_dir / filename).read_bytes() == (source_dir / filename).read_bytes()
