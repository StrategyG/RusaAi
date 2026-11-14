import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = [
    "packaging/windows/local-bundle-manifest.schema.json",
    "schemas/integrations/mcp-connection.v1.schema.json",
    "schemas/integrations/external-rag-connection.v1.schema.json",
]


def test_local_first_contract_schemas_are_valid():
    for relative_path in SCHEMAS:
        schema = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
