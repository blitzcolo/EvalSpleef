#!/usr/bin/env bash
# Regenerate the committed protobuf and gRPC stubs under src/evalspleef/hostd/_pb/.
# Run from anywhere; needs the workspace environment (grpcio-tools).
set -euo pipefail

root="$(cd "$(dirname "$0")/../../.." && pwd)"
pkg="$root/packages/evalspleef-hostd"
out="$pkg/src/evalspleef/hostd/_pb"

cd "$root"
unset SSL_CERT_FILE SSL_CERT_DIR
uv run --frozen --no-sync python -m grpc_tools.protoc \
  -I "$pkg/proto" \
  --python_out="$out" --pyi_out="$out" --grpc_python_out="$out" \
  "$pkg/proto/hostd.proto"

# protoc emits a top-level import for the sibling module; make it package-relative, and keep
# the untyped generated gRPC module and the stub (protobuf ships no types here) out of strict
# type checking.
python3 - "$out" <<'PY'
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
grpc_module = out / "hostd_pb2_grpc.py"
text = grpc_module.read_text()
grpc_module.write_text(
    text.replace("import hostd_pb2 as hostd__pb2", "from . import hostd_pb2 as hostd__pb2")
)
for name in ("hostd_pb2_grpc.py", "hostd_pb2.pyi"):
    path = out / name
    text = path.read_text()
    if not text.startswith("# mypy: ignore-errors"):
        path.write_text("# mypy: ignore-errors\n" + text)
PY
