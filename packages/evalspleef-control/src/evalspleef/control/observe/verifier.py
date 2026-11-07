"""Independent verifier sandbox: hard gates, hash locks, stale reward cleanup.

The verifier runs in its own sandbox on a separate lease with the network off
(``ctx.leases.open_verifier()``, invariants I10/I12, PRD appendix D.2). The pack's
``hidden-tests/`` is uploaded there — never into the agent sandbox — together with the
sanitised candidate tree. Every hard gate declared in task.yaml is executed and its
exit code, output and timeout state captured as core :class:`HardGate` inputs.

Gate script forms covered (studied across the packs under ``tasks/``):

- ``command:<cmd>`` — run through the sandbox shell with the working directory on the
  candidate root (debug-001/backend-001/video-001 HG3 style);
- ``hidden-tests/run.sh`` — the pack verification entry, invoked as
  ``bash hidden-tests/run.sh <candidate-root>`` (debug-001/backend-001/macos-001 style);
- ``hidden-tests/check_locks.py`` — invoked with the pack manifest and the candidate
  root (``python3 check_locks.py hidden-tests/locks.sha256 <candidate-root>``);
- any other ``hidden-tests/*.py`` whose ``pass_condition`` is ``<key> == true`` — the
  ``verify_submission.py`` family (logic-001/data-001): run with ``--json`` plus the
  candidate-path flag the pack's own ``run.sh`` uses (``--answer`` or ``--submission``)
  and read the named boolean out of the JSON report.

Test-hash locks (I12) are enforced independently of gate exit codes: the hashes of the
candidate's locked paths are recomputed from the sanitised export against the pack
manifest; a mismatch or a missing locked file is a cheat signal, not a gate opinion.
Stale ``reward.txt``/``reward.json`` planted by the candidate are deleted before the
gates run and reported as cheat signals.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from evalspleef.core.grading import HardGate
from evalspleef.spi.errors import SpleefError
from evalspleef.spi.sandbox import Sandbox

from ..taskpack.models import TaskPack

__all__ = [
    "HASH_MANIFEST_NAMES",
    "GateRun",
    "HardGateDecl",
    "HashLockEntry",
    "HashLockMismatch",
    "VerificationError",
    "VerificationOutput",
    "check_hash_locks",
    "load_hard_gates",
    "parse_hash_manifest",
    "requires_host_tools",
    "run_verification",
]

#: Stale reward files the verifier removes before running gates (I12).
REWARD_NAMES = ("reward.txt", "reward.json")

#: Candidate-path flags used by the verify_submission.py family of packs.
_ANSWER_FLAGS = ("--answer", "--submission")

_DEFAULT_GATE_TIMEOUT_S = 300.0
_EXEC_OUTPUT_LIMIT = 1 << 20


class VerificationError(SpleefError):
    """The pack's verification protocol broke (unknown gate form, unusable report)."""

    error_class = "verification"
    disposition = "manual"


@dataclass(frozen=True)
class HardGateDecl:
    """One ``hard_gates:`` entry of task.yaml."""

    gate_id: str
    kind: str
    script: str
    required: bool
    pass_condition: str
    description: str = ""
    timeout_s: float = _DEFAULT_GATE_TIMEOUT_S


@dataclass(frozen=True)
class GateRun:
    """The captured outcome of one hard gate execution."""

    gate: HardGateDecl
    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

    def hard_gate(self, evidence_uri: str | None = None) -> HardGate:
        """The core record consumed by grading."""
        return HardGate(check_id=self.gate.gate_id, passed=self.passed, evidence_uri=evidence_uri)

    @property
    def output(self) -> str:
        """Combined gate output, what check-result mapping scans for signals."""
        return f"{self.stdout}\n{self.stderr}"


@dataclass(frozen=True)
class HashLockEntry:
    """One locked path: ``relative`` resolves against the candidate tree."""

    relative: str
    sha256: str


@dataclass(frozen=True)
class HashLockMismatch:
    """A locked path whose hash does not match the pack manifest (I12 cheat signal)."""

    relative: str
    expected: str
    actual: str | None  # ``None`` means the locked file is missing.


@dataclass(frozen=True)
class VerificationOutput:
    """Everything the verifier produced: gates and planted rewards.

    Hash locks are computed control-side from the sanitised export
    (:func:`check_hash_locks`), independently of what the sandbox saw.
    """

    gates: tuple[GateRun, ...]
    planted_rewards: tuple[str, ...]

    def hard_gates(self) -> tuple[HardGate, ...]:
        return tuple(run.hard_gate() for run in self.gates)


# ---------------------------------------------------------------------------
# task.yaml parsing
# ---------------------------------------------------------------------------


def load_hard_gates(pack: TaskPack) -> tuple[HardGateDecl, ...]:
    """Parse the ``hard_gates:`` block of task.yaml."""
    raw = pack.metadata.get("hard_gates")
    if not isinstance(raw, list):
        return ()
    gates: list[HardGateDecl] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        gate_id = str(entry.get("id", ""))
        script = str(entry.get("script", ""))
        if not gate_id or not script:
            continue
        timeout = entry.get("timeout_s")
        gates.append(
            HardGateDecl(
                gate_id=gate_id,
                kind=str(entry.get("kind", "script")),
                script=script,
                required=bool(entry.get("required", True)),
                pass_condition=str(entry.get("pass_condition", "exit_code == 0")),
                description=str(entry.get("description", "")),
                timeout_s=float(timeout)
                if isinstance(timeout, int | float)
                else _DEFAULT_GATE_TIMEOUT_S,
            )
        )
    return tuple(gates)


def requires_host_tools(pack: TaskPack) -> str | None:
    """Reason this pack's gates cannot run in a generic linux verifier, if any.

    macOS / HarmonyOS packs need their native toolchains, GPU packs need a device host
    and video packs need the pinned ffmpeg image; those packs are selftest-only on a
    generic host and must be marked for host verification instead of faking results.
    """
    requirements = pack.requirements
    if str(requirements.os) != "linux":
        return f"pack os {requirements.os} needs a host toolchain"
    if requirements.gpu:
        return "pack needs a GPU host"
    if _needs_video_toolchain(pack):
        return "pack needs the pinned ffmpeg toolchain"
    return None


def _needs_video_toolchain(pack: TaskPack) -> bool:
    run_sh = pack.root / "hidden-tests" / "run.sh"
    try:
        text = run_sh.read_text(encoding="utf-8")
    except OSError:
        return False
    return "ffprobe" in text or "ffmpeg" in text


# ---------------------------------------------------------------------------
# Hash-lock manifests (I12)
# ---------------------------------------------------------------------------

HASH_MANIFEST_NAMES: tuple[str, ...] = ("hashes.json", "locks.sha256", "locks.json")


def find_hash_manifest(pack: TaskPack) -> Path | None:
    """The pack's hash-lock manifest, if it declares one."""
    hidden = pack.root / "hidden-tests"
    for name in HASH_MANIFEST_NAMES:
        candidate = hidden / name
        if candidate.is_file():
            return candidate
    return None


def parse_hash_manifest(path: Path) -> tuple[HashLockEntry, ...]:
    """Parse any of the three manifest forms into locked-path entries.

    ``hashes.json`` keys are relative to the hidden-tests directory (so
    ``../workspace/instances.json`` reaches into the candidate tree);
    ``locks.sha256`` and ``locks.json`` paths are relative to the candidate root.
    """
    if path.suffix == ".json":
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise VerificationError(f"hash manifest is not an object: {path}")
        locked = document.get("locked_files", document)
        if not isinstance(locked, dict):
            raise VerificationError(f"hash manifest has no locked_files mapping: {path}")
        return tuple(
            HashLockEntry(relative=str(rel), sha256=str(digest).lower())
            for rel, digest in sorted(locked.items())
        )
    entries: list[HashLockEntry] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise VerificationError(f"hash manifest line is malformed: {line!r} ({path})")
        entries.append(HashLockEntry(relative=parts[1], sha256=parts[0].lower()))
    return tuple(entries)


def _locked_local_path(relative: str, tree: Path) -> Path | None:
    """Map a manifest key onto the sanitised export tree, or ``None`` when verifier-side.

    ``hashes.json`` keys are hidden-tests-relative, so ``../workspace/x`` resolves to
    the candidate's ``workspace/x`` — the file the agent could have touched. Plain
    keys (``locks.sha256``/``locks.json``) are candidate-root-relative. Keys that stay
    verifier-side (the hidden test files themselves, which travel from the pack and
    never from the agent) have no candidate copy and are not lock-checked here.
    """
    posix = PurePosixPath(relative.replace("\\", "/"))
    if posix.is_absolute() or not posix.parts:
        return None
    via_hidden = PurePosixPath(os.path.normpath(str(PurePosixPath("hidden-tests") / posix)))
    if via_hidden.parts and via_hidden.parts[0] == "workspace":
        return tree.joinpath(*via_hidden.parts[1:])
    if ".." in posix.parts:
        return None
    return tree.joinpath(*posix.parts)


def check_hash_locks(
    manifest: Path,
    tree: Path,
    *,
    protected: tuple[str, ...] = (),
) -> tuple[HashLockMismatch, ...]:
    """Recompute locked-path hashes from the sanitised candidate tree (I12)."""
    mismatches: list[HashLockMismatch] = []
    for entry in parse_hash_manifest(manifest):
        local = _locked_local_path(entry.relative, tree)
        if local is None:
            continue
        if not local.is_file():
            if _is_protected(entry.relative, protected):
                mismatches.append(
                    HashLockMismatch(relative=entry.relative, expected=entry.sha256, actual=None)
                )
            continue
        actual = hashlib.sha256(local.read_bytes()).hexdigest()
        if actual != entry.sha256:
            mismatches.append(
                HashLockMismatch(relative=entry.relative, expected=entry.sha256, actual=actual)
            )
    return tuple(mismatches)


def _is_protected(relative: str, protected: tuple[str, ...]) -> bool:
    path = PurePosixPath(relative)
    return any(relative == p or PurePosixPath(p) in path.parents for p in protected)


# ---------------------------------------------------------------------------
# Gate execution
# ---------------------------------------------------------------------------

_PASS_CONDITION_KEY = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*true\s*$")


def _json_blobs(text: str) -> list[dict[str, object]]:
    """Every JSON object parseable out of verifier output (reports may be indented)."""
    decoder = json.JSONDecoder()
    blobs: list[dict[str, object]] = []
    for match in re.finditer(r"\{", text):
        try:
            value, _end = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            blobs.append(value)
    return blobs


def _flatten_booleans(value: object, prefix: str = "") -> dict[str, bool]:
    """Key-path -> boolean leaves, recursively, names joined with dots."""
    found: dict[str, bool] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            found.update(_flatten_booleans(item, path))
    elif isinstance(value, bool):
        found[prefix] = value
    return found


def _candidate_flag(pack: TaskPack) -> str:
    """The candidate-path flag the pack's own run.sh passes to its verifier script."""
    try:
        text = (pack.root / "hidden-tests" / "run.sh").read_text(encoding="utf-8")
    except OSError as exc:
        raise VerificationError(f"pack run.sh unreadable: {exc}") from exc
    for flag in _ANSWER_FLAGS:
        if flag in text:
            return flag
    raise VerificationError(
        f"cannot determine candidate flag for {pack.task_id}: run.sh mentions none of {_ANSWER_FLAGS}"
    )


def _primary_artifact(pack: TaskPack, candidate_dir: str) -> str:
    """The candidate artifact path a verify_submission gate consumes (cwd-relative)."""
    from .artifacts import artifact_decls

    decls = [decl for decl in artifact_decls(pack) if decl.required] or list(artifact_decls(pack))
    if not decls:
        raise VerificationError(
            f"pack {pack.task_id} declares no artifacts for its verifier script"
        )
    return f"{candidate_dir}/{decls[0].path}"


async def _exec(
    sandbox: Sandbox,
    cmd: list[str] | str,
    *,
    cwd: str,
    timeout_s: float,
) -> tuple[int, str, str, bool]:
    result = await sandbox.exec(
        cmd,
        cwd=cwd,
        timeout_s=timeout_s,
        max_output_bytes=_EXEC_OUTPUT_LIMIT,
    )
    return (
        result.exit_code,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
        result.timed_out,
    )


def _gate_command(
    gate: HardGateDecl,
    pack: TaskPack,
    *,
    verify_root: str,
    candidate_dir: str,
) -> tuple[list[str] | str, str]:
    """The sandbox command and working directory for one gate.

    Commands are relative to the exec working directory: some providers (the local
    subprocess double among them) map ``cwd`` onto the sandbox root but resolve bare
    absolute paths on the host, so command text never carries sandbox-absolute paths.
    ``verify_root`` stays sandbox-absolute because ``exec(cwd=...)`` maps it.
    """
    script = gate.script
    if script.startswith("command:"):
        return script[len("command:") :], f"{verify_root}/{candidate_dir}"
    if script.startswith("/") or ".." in PurePosixPath(script).parts:
        raise VerificationError(f"gate {gate.gate_id}: script escapes the pack: {script!r}")
    name = PurePosixPath(script).name
    if name == "run.sh":
        return ["bash", script, candidate_dir], verify_root
    if name == "check_locks.py":
        return ["python3", script, "hidden-tests/locks.sha256", candidate_dir], verify_root
    condition = _PASS_CONDITION_KEY.match(gate.pass_condition)
    if condition is not None and name.endswith(".py"):
        flag = _candidate_flag(pack)
        target = _primary_artifact(pack, candidate_dir)
        return ["python3", script, flag, target, "--json"], verify_root
    if gate.pass_condition.strip() == "exit_code == 0":
        return ["bash", script, candidate_dir], verify_root
    raise VerificationError(
        f"gate {gate.gate_id}: unsupported script form {script!r} "
        f"with pass_condition {gate.pass_condition!r}"
    )


async def _remove_stale_rewards(
    sandbox: Sandbox, verify_root: str, candidate_dir: str
) -> tuple[str, ...]:
    """Delete reward files the candidate planted (I12); return what was removed."""
    names = " -o -name ".join(shlex.quote(name) for name in REWARD_NAMES)
    find_cmd = f"find {shlex.quote(candidate_dir)} -type f \\( -name {names} \\) -print"
    exit_code, stdout, _stderr, _timed = await _exec(
        sandbox, find_cmd, cwd=verify_root, timeout_s=30.0
    )
    if exit_code != 0:
        return ()
    # find prints paths relative to the search root ("workspace/...") already.
    planted = tuple(line.strip() for line in stdout.splitlines() if line.strip())
    for path in planted:
        await _exec(sandbox, ["rm", "-f", path], cwd=verify_root, timeout_s=30.0)
    return planted


async def run_verification(
    sandbox: Sandbox,
    pack: TaskPack,
    *,
    verify_root: str = "/verify",
    candidate_dir: str = "workspace",
) -> VerificationOutput:
    """Upload hidden tests plus candidate, clean rewards, run every hard gate.

    Commands run relative to the exec working directory (see :func:`_gate_command`);
    ``run_verification`` expects the caller to have uploaded ``hidden-tests/`` and the
    sanitised candidate tree under ``verify_root`` (see :mod:`...observe.evaluator`);
    this function enforces the I12 hygiene that must happen inside the sandbox.
    """
    planted = await _remove_stale_rewards(sandbox, verify_root, candidate_dir)
    gates = load_hard_gates(pack)
    # Gates that share one script (logic-001/data-001 point HG1-HG3 at the same
    # verify_submission.py) execute once; each gate reads its own boolean key.
    shared: dict[str, tuple[int, str, str, bool]] = {}
    runs: list[GateRun] = []
    for gate in gates:
        cmd, cwd = _gate_command(gate, pack, verify_root=verify_root, candidate_dir=candidate_dir)
        cache_key = json.dumps(cmd, sort_keys=True)
        if cache_key not in shared:
            shared[cache_key] = await _exec(sandbox, cmd, cwd=cwd, timeout_s=gate.timeout_s)
        exit_code, stdout, stderr, timed_out = shared[cache_key]
        runs.append(_gate_result(gate, exit_code, stdout, stderr, timed_out))
    return VerificationOutput(gates=tuple(runs), planted_rewards=planted)


def _gate_result(
    gate: HardGateDecl,
    exit_code: int,
    stdout: str,
    stderr: str,
    timed_out: bool,
) -> GateRun:
    """Turn one captured execution into a gate result (two-state, I12/C.1)."""
    if timed_out:
        return GateRun(
            gate=gate,
            passed=False,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
        )
    condition = _PASS_CONDITION_KEY.match(gate.pass_condition)
    if condition is not None and not gate.script.startswith("command:"):
        key = condition.group(1)
        # Exit 2 is the packs' own "usage/environment error" code: our invocation is
        # broken, not the candidate's answer, so this is an infrastructure failure.
        if exit_code == 2 and not _json_blobs(stdout):
            raise VerificationError(
                f"gate {gate.gate_id}: verifier script exited 2 without a report: "
                f"{stderr.strip()[:300]}"
            )
        passed = False
        for blob in _json_blobs(stdout):
            booleans = _flatten_booleans(blob)
            if key in booleans:
                passed = booleans[key]
                break
        return GateRun(
            gate=gate,
            passed=passed,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=False,
        )
    return GateRun(
        gate=gate,
        passed=exit_code == 0,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
    )
