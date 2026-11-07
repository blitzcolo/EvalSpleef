# mypy: ignore-errors
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Resources(_message.Message):
    __slots__ = ("cpus", "memory_mb", "disk_mb", "gpus", "gpu_type")
    CPUS_FIELD_NUMBER: _ClassVar[int]
    MEMORY_MB_FIELD_NUMBER: _ClassVar[int]
    DISK_MB_FIELD_NUMBER: _ClassVar[int]
    GPUS_FIELD_NUMBER: _ClassVar[int]
    GPU_TYPE_FIELD_NUMBER: _ClassVar[int]
    cpus: float
    memory_mb: int
    disk_mb: int
    gpus: int
    gpu_type: str
    def __init__(self, cpus: _Optional[float] = ..., memory_mb: _Optional[int] = ..., disk_mb: _Optional[int] = ..., gpus: _Optional[int] = ..., gpu_type: _Optional[str] = ...) -> None: ...

class NetworkPolicy(_message.Message):
    __slots__ = ("mode", "allow_hosts", "allow_cidrs", "via_proxy")
    MODE_FIELD_NUMBER: _ClassVar[int]
    ALLOW_HOSTS_FIELD_NUMBER: _ClassVar[int]
    ALLOW_CIDRS_FIELD_NUMBER: _ClassVar[int]
    VIA_PROXY_FIELD_NUMBER: _ClassVar[int]
    mode: str
    allow_hosts: _containers.RepeatedScalarFieldContainer[str]
    allow_cidrs: _containers.RepeatedScalarFieldContainer[str]
    via_proxy: str
    def __init__(self, mode: _Optional[str] = ..., allow_hosts: _Optional[_Iterable[str]] = ..., allow_cidrs: _Optional[_Iterable[str]] = ..., via_proxy: _Optional[str] = ...) -> None: ...

class TemplateRef(_message.Message):
    __slots__ = ("kind", "ref", "digest")
    KIND_FIELD_NUMBER: _ClassVar[int]
    REF_FIELD_NUMBER: _ClassVar[int]
    DIGEST_FIELD_NUMBER: _ClassVar[int]
    kind: str
    ref: str
    digest: str
    def __init__(self, kind: _Optional[str] = ..., ref: _Optional[str] = ..., digest: _Optional[str] = ...) -> None: ...

class SandboxSpec(_message.Message):
    __slots__ = ("trial_id", "lease_id", "role", "os", "arch", "template", "resources", "network", "run_as_user", "env", "labels", "max_lifetime_s", "gui")
    class EnvEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    LEASE_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    OS_FIELD_NUMBER: _ClassVar[int]
    ARCH_FIELD_NUMBER: _ClassVar[int]
    TEMPLATE_FIELD_NUMBER: _ClassVar[int]
    RESOURCES_FIELD_NUMBER: _ClassVar[int]
    NETWORK_FIELD_NUMBER: _ClassVar[int]
    RUN_AS_USER_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    MAX_LIFETIME_S_FIELD_NUMBER: _ClassVar[int]
    GUI_FIELD_NUMBER: _ClassVar[int]
    trial_id: str
    lease_id: str
    role: str
    os: str
    arch: str
    template: TemplateRef
    resources: Resources
    network: NetworkPolicy
    run_as_user: str
    env: _containers.ScalarMap[str, str]
    labels: _containers.ScalarMap[str, str]
    max_lifetime_s: int
    gui: bool
    def __init__(self, trial_id: _Optional[str] = ..., lease_id: _Optional[str] = ..., role: _Optional[str] = ..., os: _Optional[str] = ..., arch: _Optional[str] = ..., template: _Optional[_Union[TemplateRef, _Mapping]] = ..., resources: _Optional[_Union[Resources, _Mapping]] = ..., network: _Optional[_Union[NetworkPolicy, _Mapping]] = ..., run_as_user: _Optional[str] = ..., env: _Optional[_Mapping[str, str]] = ..., labels: _Optional[_Mapping[str, str]] = ..., max_lifetime_s: _Optional[int] = ..., gui: _Optional[bool] = ...) -> None: ...

class SandboxCapabilities(_message.Message):
    __slots__ = ("os_families", "arches", "isolation", "net_modes", "allowlist_kinds", "dynamic_network_policy", "streaming", "stdin", "snapshot", "snapshot_fanout", "pause_resume", "gui", "gpu", "port_expose", "host_mounts", "enforces_limits", "max_concurrency", "max_lifetime_s", "max_resources", "typical_start_s", "reset_cost_s")
    OS_FAMILIES_FIELD_NUMBER: _ClassVar[int]
    ARCHES_FIELD_NUMBER: _ClassVar[int]
    ISOLATION_FIELD_NUMBER: _ClassVar[int]
    NET_MODES_FIELD_NUMBER: _ClassVar[int]
    ALLOWLIST_KINDS_FIELD_NUMBER: _ClassVar[int]
    DYNAMIC_NETWORK_POLICY_FIELD_NUMBER: _ClassVar[int]
    STREAMING_FIELD_NUMBER: _ClassVar[int]
    STDIN_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_FANOUT_FIELD_NUMBER: _ClassVar[int]
    PAUSE_RESUME_FIELD_NUMBER: _ClassVar[int]
    GUI_FIELD_NUMBER: _ClassVar[int]
    GPU_FIELD_NUMBER: _ClassVar[int]
    PORT_EXPOSE_FIELD_NUMBER: _ClassVar[int]
    HOST_MOUNTS_FIELD_NUMBER: _ClassVar[int]
    ENFORCES_LIMITS_FIELD_NUMBER: _ClassVar[int]
    MAX_CONCURRENCY_FIELD_NUMBER: _ClassVar[int]
    MAX_LIFETIME_S_FIELD_NUMBER: _ClassVar[int]
    MAX_RESOURCES_FIELD_NUMBER: _ClassVar[int]
    TYPICAL_START_S_FIELD_NUMBER: _ClassVar[int]
    RESET_COST_S_FIELD_NUMBER: _ClassVar[int]
    os_families: _containers.RepeatedScalarFieldContainer[str]
    arches: _containers.RepeatedScalarFieldContainer[str]
    isolation: int
    net_modes: _containers.RepeatedScalarFieldContainer[str]
    allowlist_kinds: _containers.RepeatedScalarFieldContainer[str]
    dynamic_network_policy: bool
    streaming: bool
    stdin: bool
    snapshot: str
    snapshot_fanout: bool
    pause_resume: bool
    gui: str
    gpu: bool
    port_expose: bool
    host_mounts: bool
    enforces_limits: _containers.RepeatedScalarFieldContainer[str]
    max_concurrency: int
    max_lifetime_s: int
    max_resources: Resources
    typical_start_s: float
    reset_cost_s: float
    def __init__(self, os_families: _Optional[_Iterable[str]] = ..., arches: _Optional[_Iterable[str]] = ..., isolation: _Optional[int] = ..., net_modes: _Optional[_Iterable[str]] = ..., allowlist_kinds: _Optional[_Iterable[str]] = ..., dynamic_network_policy: _Optional[bool] = ..., streaming: _Optional[bool] = ..., stdin: _Optional[bool] = ..., snapshot: _Optional[str] = ..., snapshot_fanout: _Optional[bool] = ..., pause_resume: _Optional[bool] = ..., gui: _Optional[str] = ..., gpu: _Optional[bool] = ..., port_expose: _Optional[bool] = ..., host_mounts: _Optional[bool] = ..., enforces_limits: _Optional[_Iterable[str]] = ..., max_concurrency: _Optional[int] = ..., max_lifetime_s: _Optional[int] = ..., max_resources: _Optional[_Union[Resources, _Mapping]] = ..., typical_start_s: _Optional[float] = ..., reset_cost_s: _Optional[float] = ...) -> None: ...

class InvariantEntry(_message.Message):
    __slots__ = ("how", "probe", "waived", "reason", "compensating", "approved_by", "expires")
    HOW_FIELD_NUMBER: _ClassVar[int]
    PROBE_FIELD_NUMBER: _ClassVar[int]
    WAIVED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    COMPENSATING_FIELD_NUMBER: _ClassVar[int]
    APPROVED_BY_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_FIELD_NUMBER: _ClassVar[int]
    how: str
    probe: str
    waived: bool
    reason: str
    compensating: str
    approved_by: str
    expires: str
    def __init__(self, how: _Optional[str] = ..., probe: _Optional[str] = ..., waived: _Optional[bool] = ..., reason: _Optional[str] = ..., compensating: _Optional[str] = ..., approved_by: _Optional[str] = ..., expires: _Optional[str] = ...) -> None: ...

class IsolationProfile(_message.Message):
    __slots__ = ("provider", "declared_isolation", "verified_isolation", "invariants")
    class InvariantsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: InvariantEntry
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[InvariantEntry, _Mapping]] = ...) -> None: ...
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    DECLARED_ISOLATION_FIELD_NUMBER: _ClassVar[int]
    VERIFIED_ISOLATION_FIELD_NUMBER: _ClassVar[int]
    INVARIANTS_FIELD_NUMBER: _ClassVar[int]
    provider: str
    declared_isolation: int
    verified_isolation: int
    invariants: _containers.MessageMap[str, InvariantEntry]
    def __init__(self, provider: _Optional[str] = ..., declared_isolation: _Optional[int] = ..., verified_isolation: _Optional[int] = ..., invariants: _Optional[_Mapping[str, InvariantEntry]] = ...) -> None: ...

class JsonDocument(_message.Message):
    __slots__ = ("json",)
    JSON_FIELD_NUMBER: _ClassVar[int]
    json: str
    def __init__(self, json: _Optional[str] = ...) -> None: ...

class Empty(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ProviderRef(_message.Message):
    __slots__ = ("provider",)
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    provider: str
    def __init__(self, provider: _Optional[str] = ...) -> None: ...

class SandboxRef(_message.Message):
    __slots__ = ("provider", "sandbox_id")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    provider: str
    sandbox_id: str
    def __init__(self, provider: _Optional[str] = ..., sandbox_id: _Optional[str] = ...) -> None: ...

class CreateRequest(_message.Message):
    __slots__ = ("provider", "spec", "snapshot_id")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    provider: str
    spec: SandboxSpec
    snapshot_id: str
    def __init__(self, provider: _Optional[str] = ..., spec: _Optional[_Union[SandboxSpec, _Mapping]] = ..., snapshot_id: _Optional[str] = ...) -> None: ...

class CreateResponse(_message.Message):
    __slots__ = ("sandbox_id", "spec")
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    SPEC_FIELD_NUMBER: _ClassVar[int]
    sandbox_id: str
    spec: SandboxSpec
    def __init__(self, sandbox_id: _Optional[str] = ..., spec: _Optional[_Union[SandboxSpec, _Mapping]] = ...) -> None: ...

class ExecRequest(_message.Message):
    __slots__ = ("provider", "sandbox_id", "shell_command", "argv", "cwd", "env", "user", "timeout_s", "stdin", "max_output_bytes")
    class EnvEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    SHELL_COMMAND_FIELD_NUMBER: _ClassVar[int]
    ARGV_FIELD_NUMBER: _ClassVar[int]
    CWD_FIELD_NUMBER: _ClassVar[int]
    ENV_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_S_FIELD_NUMBER: _ClassVar[int]
    STDIN_FIELD_NUMBER: _ClassVar[int]
    MAX_OUTPUT_BYTES_FIELD_NUMBER: _ClassVar[int]
    provider: str
    sandbox_id: str
    shell_command: str
    argv: _containers.RepeatedScalarFieldContainer[str]
    cwd: str
    env: _containers.ScalarMap[str, str]
    user: str
    timeout_s: float
    stdin: bytes
    max_output_bytes: int
    def __init__(self, provider: _Optional[str] = ..., sandbox_id: _Optional[str] = ..., shell_command: _Optional[str] = ..., argv: _Optional[_Iterable[str]] = ..., cwd: _Optional[str] = ..., env: _Optional[_Mapping[str, str]] = ..., user: _Optional[str] = ..., timeout_s: _Optional[float] = ..., stdin: _Optional[bytes] = ..., max_output_bytes: _Optional[int] = ...) -> None: ...

class OutputChunk(_message.Message):
    __slots__ = ("stream", "data")
    STREAM_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    stream: str
    data: bytes
    def __init__(self, stream: _Optional[str] = ..., data: _Optional[bytes] = ...) -> None: ...

class ExecSummary(_message.Message):
    __slots__ = ("exit_code", "timed_out", "duration_s", "truncated")
    EXIT_CODE_FIELD_NUMBER: _ClassVar[int]
    TIMED_OUT_FIELD_NUMBER: _ClassVar[int]
    DURATION_S_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    exit_code: int
    timed_out: bool
    duration_s: float
    truncated: bool
    def __init__(self, exit_code: _Optional[int] = ..., timed_out: _Optional[bool] = ..., duration_s: _Optional[float] = ..., truncated: _Optional[bool] = ...) -> None: ...

class ExecEvent(_message.Message):
    __slots__ = ("live", "final_chunk", "summary")
    LIVE_FIELD_NUMBER: _ClassVar[int]
    FINAL_CHUNK_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    live: OutputChunk
    final_chunk: OutputChunk
    summary: ExecSummary
    def __init__(self, live: _Optional[_Union[OutputChunk, _Mapping]] = ..., final_chunk: _Optional[_Union[OutputChunk, _Mapping]] = ..., summary: _Optional[_Union[ExecSummary, _Mapping]] = ...) -> None: ...

class TransferHeader(_message.Message):
    __slots__ = ("provider", "sandbox_id", "remote")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    REMOTE_FIELD_NUMBER: _ClassVar[int]
    provider: str
    sandbox_id: str
    remote: str
    def __init__(self, provider: _Optional[str] = ..., sandbox_id: _Optional[str] = ..., remote: _Optional[str] = ...) -> None: ...

class TransferChunk(_message.Message):
    __slots__ = ("index", "data", "sha256")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    index: int
    data: bytes
    sha256: str
    def __init__(self, index: _Optional[int] = ..., data: _Optional[bytes] = ..., sha256: _Optional[str] = ...) -> None: ...

class TransferTrailer(_message.Message):
    __slots__ = ("chunk_count", "total_bytes", "sha256")
    CHUNK_COUNT_FIELD_NUMBER: _ClassVar[int]
    TOTAL_BYTES_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    chunk_count: int
    total_bytes: int
    sha256: str
    def __init__(self, chunk_count: _Optional[int] = ..., total_bytes: _Optional[int] = ..., sha256: _Optional[str] = ...) -> None: ...

class UploadMessage(_message.Message):
    __slots__ = ("header", "chunk", "trailer")
    HEADER_FIELD_NUMBER: _ClassVar[int]
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    TRAILER_FIELD_NUMBER: _ClassVar[int]
    header: TransferHeader
    chunk: TransferChunk
    trailer: TransferTrailer
    def __init__(self, header: _Optional[_Union[TransferHeader, _Mapping]] = ..., chunk: _Optional[_Union[TransferChunk, _Mapping]] = ..., trailer: _Optional[_Union[TransferTrailer, _Mapping]] = ...) -> None: ...

class UploadAck(_message.Message):
    __slots__ = ("total_bytes",)
    TOTAL_BYTES_FIELD_NUMBER: _ClassVar[int]
    total_bytes: int
    def __init__(self, total_bytes: _Optional[int] = ...) -> None: ...

class DownloadRequest(_message.Message):
    __slots__ = ("provider", "sandbox_id", "remote", "max_bytes")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    REMOTE_FIELD_NUMBER: _ClassVar[int]
    MAX_BYTES_FIELD_NUMBER: _ClassVar[int]
    provider: str
    sandbox_id: str
    remote: str
    max_bytes: int
    def __init__(self, provider: _Optional[str] = ..., sandbox_id: _Optional[str] = ..., remote: _Optional[str] = ..., max_bytes: _Optional[int] = ...) -> None: ...

class DownloadMessage(_message.Message):
    __slots__ = ("chunk", "trailer")
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    TRAILER_FIELD_NUMBER: _ClassVar[int]
    chunk: TransferChunk
    trailer: TransferTrailer
    def __init__(self, chunk: _Optional[_Union[TransferChunk, _Mapping]] = ..., trailer: _Optional[_Union[TransferTrailer, _Mapping]] = ...) -> None: ...

class LabelQuery(_message.Message):
    __slots__ = ("provider", "labels")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    provider: str
    labels: _containers.ScalarMap[str, str]
    def __init__(self, provider: _Optional[str] = ..., labels: _Optional[_Mapping[str, str]] = ...) -> None: ...

class ListResponse(_message.Message):
    __slots__ = ("entries",)
    ENTRIES_FIELD_NUMBER: _ClassVar[int]
    entries: _containers.RepeatedCompositeFieldContainer[JsonDocument]
    def __init__(self, entries: _Optional[_Iterable[_Union[JsonDocument, _Mapping]]] = ...) -> None: ...

class ReapRequest(_message.Message):
    __slots__ = ("provider", "labels", "older_than_s")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    OLDER_THAN_S_FIELD_NUMBER: _ClassVar[int]
    provider: str
    labels: _containers.ScalarMap[str, str]
    older_than_s: int
    def __init__(self, provider: _Optional[str] = ..., labels: _Optional[_Mapping[str, str]] = ..., older_than_s: _Optional[int] = ...) -> None: ...

class ReapResponse(_message.Message):
    __slots__ = ("destroyed",)
    DESTROYED_FIELD_NUMBER: _ClassVar[int]
    destroyed: int
    def __init__(self, destroyed: _Optional[int] = ...) -> None: ...

class SnapshotRequest(_message.Message):
    __slots__ = ("provider", "sandbox_id", "name")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    provider: str
    sandbox_id: str
    name: str
    def __init__(self, provider: _Optional[str] = ..., sandbox_id: _Optional[str] = ..., name: _Optional[str] = ...) -> None: ...

class SnapshotResponse(_message.Message):
    __slots__ = ("snapshot_id",)
    SNAPSHOT_ID_FIELD_NUMBER: _ClassVar[int]
    snapshot_id: str
    def __init__(self, snapshot_id: _Optional[str] = ...) -> None: ...

class SetNetworkRequest(_message.Message):
    __slots__ = ("provider", "sandbox_id", "policy")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    POLICY_FIELD_NUMBER: _ClassVar[int]
    provider: str
    sandbox_id: str
    policy: NetworkPolicy
    def __init__(self, provider: _Optional[str] = ..., sandbox_id: _Optional[str] = ..., policy: _Optional[_Union[NetworkPolicy, _Mapping]] = ...) -> None: ...

class ExposePortRequest(_message.Message):
    __slots__ = ("provider", "sandbox_id", "port")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    PORT_FIELD_NUMBER: _ClassVar[int]
    provider: str
    sandbox_id: str
    port: int
    def __init__(self, provider: _Optional[str] = ..., sandbox_id: _Optional[str] = ..., port: _Optional[int] = ...) -> None: ...

class EndpointResponse(_message.Message):
    __slots__ = ("endpoint",)
    ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    endpoint: str
    def __init__(self, endpoint: _Optional[str] = ...) -> None: ...

class ProviderSlots(_message.Message):
    __slots__ = ("provider", "total", "used", "reserved")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    TOTAL_FIELD_NUMBER: _ClassVar[int]
    USED_FIELD_NUMBER: _ClassVar[int]
    RESERVED_FIELD_NUMBER: _ClassVar[int]
    provider: str
    total: int
    used: int
    reserved: int
    def __init__(self, provider: _Optional[str] = ..., total: _Optional[int] = ..., used: _Optional[int] = ..., reserved: _Optional[int] = ...) -> None: ...

class HostInventory(_message.Message):
    __slots__ = ("host_id", "mode", "slots", "sandbox_ids", "hostd_version")
    HOST_ID_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    SLOTS_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_IDS_FIELD_NUMBER: _ClassVar[int]
    HOSTD_VERSION_FIELD_NUMBER: _ClassVar[int]
    host_id: str
    mode: str
    slots: _containers.RepeatedCompositeFieldContainer[ProviderSlots]
    sandbox_ids: _containers.RepeatedScalarFieldContainer[str]
    hostd_version: str
    def __init__(self, host_id: _Optional[str] = ..., mode: _Optional[str] = ..., slots: _Optional[_Iterable[_Union[ProviderSlots, _Mapping]]] = ..., sandbox_ids: _Optional[_Iterable[str]] = ..., hostd_version: _Optional[str] = ...) -> None: ...

class ModeRequest(_message.Message):
    __slots__ = ("enabled",)
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    enabled: bool
    def __init__(self, enabled: _Optional[bool] = ...) -> None: ...

class HostMode(_message.Message):
    __slots__ = ("mode",)
    MODE_FIELD_NUMBER: _ClassVar[int]
    mode: str
    def __init__(self, mode: _Optional[str] = ...) -> None: ...

class ProviderInfo(_message.Message):
    __slots__ = ("name", "adapter", "capabilities")
    NAME_FIELD_NUMBER: _ClassVar[int]
    ADAPTER_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    name: str
    adapter: str
    capabilities: SandboxCapabilities
    def __init__(self, name: _Optional[str] = ..., adapter: _Optional[str] = ..., capabilities: _Optional[_Union[SandboxCapabilities, _Mapping]] = ...) -> None: ...

class HostInfo(_message.Message):
    __slots__ = ("machine", "os", "arch", "cpu_count", "memory_mb", "disk_free_mb", "hostd_version", "providers")
    MACHINE_FIELD_NUMBER: _ClassVar[int]
    OS_FIELD_NUMBER: _ClassVar[int]
    ARCH_FIELD_NUMBER: _ClassVar[int]
    CPU_COUNT_FIELD_NUMBER: _ClassVar[int]
    MEMORY_MB_FIELD_NUMBER: _ClassVar[int]
    DISK_FREE_MB_FIELD_NUMBER: _ClassVar[int]
    HOSTD_VERSION_FIELD_NUMBER: _ClassVar[int]
    PROVIDERS_FIELD_NUMBER: _ClassVar[int]
    machine: str
    os: str
    arch: str
    cpu_count: int
    memory_mb: int
    disk_free_mb: int
    hostd_version: str
    providers: _containers.RepeatedCompositeFieldContainer[ProviderInfo]
    def __init__(self, machine: _Optional[str] = ..., os: _Optional[str] = ..., arch: _Optional[str] = ..., cpu_count: _Optional[int] = ..., memory_mb: _Optional[int] = ..., disk_free_mb: _Optional[int] = ..., hostd_version: _Optional[str] = ..., providers: _Optional[_Iterable[_Union[ProviderInfo, _Mapping]]] = ...) -> None: ...

class RegisterRequest(_message.Message):
    __slots__ = ("host_id", "token", "csr_pem", "service_address", "service_names", "info")
    HOST_ID_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    CSR_PEM_FIELD_NUMBER: _ClassVar[int]
    SERVICE_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    SERVICE_NAMES_FIELD_NUMBER: _ClassVar[int]
    INFO_FIELD_NUMBER: _ClassVar[int]
    host_id: str
    token: str
    csr_pem: bytes
    service_address: str
    service_names: _containers.RepeatedScalarFieldContainer[str]
    info: HostInfo
    def __init__(self, host_id: _Optional[str] = ..., token: _Optional[str] = ..., csr_pem: _Optional[bytes] = ..., service_address: _Optional[str] = ..., service_names: _Optional[_Iterable[str]] = ..., info: _Optional[_Union[HostInfo, _Mapping]] = ...) -> None: ...

class RegisterResponse(_message.Message):
    __slots__ = ("cert_pem", "ca_pem", "not_after")
    CERT_PEM_FIELD_NUMBER: _ClassVar[int]
    CA_PEM_FIELD_NUMBER: _ClassVar[int]
    NOT_AFTER_FIELD_NUMBER: _ClassVar[int]
    cert_pem: bytes
    ca_pem: bytes
    not_after: float
    def __init__(self, cert_pem: _Optional[bytes] = ..., ca_pem: _Optional[bytes] = ..., not_after: _Optional[float] = ...) -> None: ...

class HeartbeatRequest(_message.Message):
    __slots__ = ("host_id", "inventory", "disk_free_mb", "load_1m")
    HOST_ID_FIELD_NUMBER: _ClassVar[int]
    INVENTORY_FIELD_NUMBER: _ClassVar[int]
    DISK_FREE_MB_FIELD_NUMBER: _ClassVar[int]
    LOAD_1M_FIELD_NUMBER: _ClassVar[int]
    host_id: str
    inventory: HostInventory
    disk_free_mb: int
    load_1m: float
    def __init__(self, host_id: _Optional[str] = ..., inventory: _Optional[_Union[HostInventory, _Mapping]] = ..., disk_free_mb: _Optional[int] = ..., load_1m: _Optional[float] = ...) -> None: ...

class HeartbeatResponse(_message.Message):
    __slots__ = ("accepted",)
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    def __init__(self, accepted: _Optional[bool] = ...) -> None: ...

class RenewRequest(_message.Message):
    __slots__ = ("host_id", "csr_pem")
    HOST_ID_FIELD_NUMBER: _ClassVar[int]
    CSR_PEM_FIELD_NUMBER: _ClassVar[int]
    host_id: str
    csr_pem: bytes
    def __init__(self, host_id: _Optional[str] = ..., csr_pem: _Optional[bytes] = ...) -> None: ...

class ReclaimedSandbox(_message.Message):
    __slots__ = ("provider", "sandbox_id", "trial_id", "reason")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    SANDBOX_ID_FIELD_NUMBER: _ClassVar[int]
    TRIAL_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    provider: str
    sandbox_id: str
    trial_id: str
    reason: str
    def __init__(self, provider: _Optional[str] = ..., sandbox_id: _Optional[str] = ..., trial_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class ReclaimedReport(_message.Message):
    __slots__ = ("host_id", "reclaimed")
    HOST_ID_FIELD_NUMBER: _ClassVar[int]
    RECLAIMED_FIELD_NUMBER: _ClassVar[int]
    host_id: str
    reclaimed: _containers.RepeatedCompositeFieldContainer[ReclaimedSandbox]
    def __init__(self, host_id: _Optional[str] = ..., reclaimed: _Optional[_Iterable[_Union[ReclaimedSandbox, _Mapping]]] = ...) -> None: ...
