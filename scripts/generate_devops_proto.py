"""Generate the versioned DevOps WorkflowControl protobuf and gRPC Python stubs."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTO_DIR = ROOT / "contracts" / "proto" / "rusa" / "devops" / "v1"
PROTO_FILE = PROTO_DIR / "workflow.proto"
OUTPUT_DIR = ROOT / "rusa_agents" / "devops" / "generated"


def main() -> None:
    try:
        import grpc_tools
        from grpc_tools import protoc
    except ImportError as exc:
        raise SystemExit(
            "Install grpcio-tools before generating WorkflowControl stubs"
        ) from exc

    if not PROTO_FILE.is_file():
        raise SystemExit(f"protobuf source not found: {PROTO_FILE}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "__init__.py").write_text(
        '"""Generated RUSA DevOps protobuf modules."""\n',
        encoding="utf-8",
    )
    well_known = Path(grpc_tools.__file__).resolve().parent / "_proto"
    arguments = [
        "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"-I{well_known}",
        f"--python_out={OUTPUT_DIR}",
        f"--grpc_python_out={OUTPUT_DIR}",
        "workflow.proto",
    ]
    result = protoc.main(arguments)
    if result != 0:
        raise SystemExit(f"grpc_tools.protoc failed with exit code {result}")

    grpc_stub = OUTPUT_DIR / "workflow_pb2_grpc.py"
    source = grpc_stub.read_text(encoding="utf-8")
    source = source.replace(
        "import workflow_pb2 as workflow__pb2",
        "from . import workflow_pb2 as workflow__pb2",
    )
    if "import workflow_pb2 as workflow__pb2" in source:
        raise SystemExit("failed to make generated gRPC stub package-relative")
    grpc_stub.write_text(source, encoding="utf-8")
    print(f"Generated WorkflowControl stubs in {OUTPUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
