"""In-memory fakes of the novita-sandbox / e2b SDK surface used by the adapter.

The fakes are installed by patching the SDK entry points the adapter modules
imported (see :func:`core.patch_adapter_module`) and execute commands as real
local subprocesses inside per-sandbox temporary directories. They exist so the
adapter logic — and the conformance kit's fake targets — can be exercised
without a real Novita account or E2B cluster: **nothing running against these
fakes is verified on a real host**.
"""

from evalspleef_sbx_e2b_family.fakes.core import (
    FakeCommandExitException,
    FakeCommandHandle,
    FakeCommandResult,
    FakeE2BCluster,
    FakeFileNotFoundException,
    FakeNovita,
    FakeRateLimitException,
    FakeSandbox,
    FakeSandboxException,
    FakeSandboxNotFoundException,
    make_fake_novita_class,
    make_fake_sandbox_class,
    patch_adapter_module,
    unpatch_adapter_module,
)

__all__ = [
    "FakeCommandExitException",
    "FakeCommandHandle",
    "FakeCommandResult",
    "FakeE2BCluster",
    "FakeFileNotFoundException",
    "FakeNovita",
    "FakeRateLimitException",
    "FakeSandbox",
    "FakeSandboxException",
    "FakeSandboxNotFoundException",
    "make_fake_novita_class",
    "make_fake_sandbox_class",
    "patch_adapter_module",
    "unpatch_adapter_module",
]
