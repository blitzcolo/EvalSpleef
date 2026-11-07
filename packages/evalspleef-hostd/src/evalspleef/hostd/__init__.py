"""Host agent (hostd) and RemoteProvider: local providers exposed to the control plane over mTLS gRPC."""

import os

# Local providers fork subprocesses (commands, docker CLI) while gRPC threads run. gRPC's fork
# handlers would write diagnostics into the child's stderr, which sandbox output must not carry.
# The variable is read when grpc initialises, so it has to be set before the first grpc import.
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
