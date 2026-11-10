"""Export the checked-in JSON Schemas for RUSA AI platform contracts."""
from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path

from rusa_agents.platform_contracts import (
    PLATFORM_SCHEMA_MODELS,
    normalize_contract_schema,
    platform_contract_schema,
)


def export_schemas(output_dir: Path, *, check: bool = False) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, (model, schema_id) in PLATFORM_SCHEMA_MODELS.items():
        path = output_dir / filename
        rendered = json.dumps(
            platform_contract_schema(model, schema_id),
            indent=2,
            sort_keys=True,
        ) + "\n"
        if check:
            if not path.is_file():
                raise SystemExit(f"platform contract schema is missing: {path}")
            actual = path.read_text(encoding="utf-8")
            try:
                actual_schema = json.loads(actual)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid platform contract schema JSON: {path}") from exc
            expected_schema = platform_contract_schema(model, schema_id)
            if normalize_contract_schema(actual_schema) != normalize_contract_schema(expected_schema):
                diff = difflib.unified_diff(
                    json.dumps(actual_schema, indent=2, sort_keys=True).splitlines(),
                    json.dumps(expected_schema, indent=2, sort_keys=True).splitlines(),
                    fromfile=str(path),
                    tofile="generated",
                    lineterm="",
                )
                print("\n".join(list(diff)[:120]))
                raise SystemExit(f"platform contract schema is stale: {path}")
        else:
            path.write_text(rendered, encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("schemas/platform"),
        help="directory for generated JSON Schemas",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if checked-in schemas differ from the Pydantic models",
    )
    args = parser.parse_args()
    for path in export_schemas(args.output_dir, check=args.check):
        print(path)


if __name__ == "__main__":
    main()
