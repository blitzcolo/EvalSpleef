"""Sandbox preparation before the agent runs (PRD appendix B.2a).

S3 canary: the token is written into the starter files named by the pack's ``canary.md`` and the
placement hashes are recorded. S4 identity hygiene: the sandbox shows a neutral user, hostname
and environment. S5 isolation preflight: every executable probe of the provider's isolation
profile for the preflight invariants I1-I9 has to exit 0.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evalspleef.core.canary import CanaryPlacement, build_placements, inject_comment
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import Sandbox

from ..taskpack.models import TaskPack
from .errors import CanaryInjectionError, IdentityHygieneError
from .types import WorkspacePaths

__all__ = [
    "PREFLIGHT_INVARIANTS",
    "ProbeResult",
    "StagedWorkspace",
    "check_identity_hygiene",
    "parse_canary_targets",
    "run_isolation_probes",
    "stage_workspace",
    "upload_and_verify",
]

PROBE_TIMEOUT_S = 30.0
PREFLIGHT_INVARIANTS = tuple(f"I{number}" for number in range(1, 10))
"""I1-I9 are the preflight invariants (PRD appendix D.2); I10-I16 hold at run time."""

_EXCLUDED_ROOTS = frozenset({"tests", "hidden-tests", "answers"})
_PREFIXES = ("", "starter/", "workspace/")
_PATH_TOKEN = re.compile(r"[\w.\-]+(?:/[\w.\-]+)*")
_HAS_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]*$")
_PLACEMENT_HEADING = re.compile(r"写入位置|埋点位置|placements?|injection", re.IGNORECASE)
_NEW_FILE = re.compile(r"新增|new file|\(new\)", re.IGNORECASE)
_HTML_SUFFIXES = frozenset({".md", ".html", ".htm", ".xml", ".svg"})
_SLASH_SUFFIXES = frozenset(
    {".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".c", ".cc", ".cpp", ".h"}
    | {".hpp", ".rs", ".swift", ".scss", ".css"}
)
_EVAL_ENV_NAME = re.compile(r"^(TASK_ID|MODEL|BATCH|EVAL|HARBOR)", re.IGNORECASE)
_CREDENTIAL = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_MIN_IDENTIFIER_LEN = 4


@dataclass(frozen=True)
class StagedWorkspace:
    """The starter files with the canary written in, ready to be uploaded to any sandbox."""

    directory: Path
    placements: tuple[CanaryPlacement, ...]
    hashes: dict[str, str]
    """Workspace-relative path -> sha256 of the file as uploaded."""
    instruction: str


@dataclass(frozen=True)
class ProbeResult:
    invariant: str
    probe: str
    exit_code: int
    timed_out: bool

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "invariant": self.invariant,
            "probe": self.probe,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "passed": self.passed,
        }


# -- S3: canary -------------------------------------------------------------------------


def _placement_lines(manifest: str) -> list[str]:
    """The lines under the manifest heading that lists where the token is written."""
    lines = manifest.splitlines()
    for start, line in enumerate(lines):
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if not heading or not _PLACEMENT_HEADING.search(heading.group(2)):
            continue
        section: list[str] = []
        for later in lines[start + 1 :]:
            deeper = re.match(r"^(#{1,6})\s", later)
            if deeper and len(deeper.group(1)) <= len(heading.group(1)):
                break
            section.append(later)
        return section
    return []


def _resolve_target(candidate: str, stage: Path, creating: bool) -> str | None:
    """A workspace-relative path for a path-like word of the manifest, if it is usable."""
    for prefix in _PREFIXES:
        if not candidate.startswith(prefix):
            continue
        relative = candidate[len(prefix) :]
        parts = relative.split("/")
        if not relative or ".." in parts or parts[0] in _EXCLUDED_ROOTS:
            continue
        if (stage / relative).is_file():
            return relative
    if creating:
        relative = candidate
        for prefix in _PREFIXES[1:]:
            if candidate.startswith(prefix):
                relative = candidate[len(prefix) :]
        parts = relative.split("/")
        if ".." not in parts and parts[0] not in _EXCLUDED_ROOTS:
            return relative
    return None


def parse_canary_targets(manifest: str, stage: Path) -> list[tuple[str, bool]]:
    """Workspace files named in the placement table of ``canary.md``: ``(path, create)``.

    Existing starter files are injected in place; files marked as new in their table row are
    created. Anything that is not a file of the workspace (instruction text, result store) is
    skipped, as are the paths the manifest forbids (``tests/``, ``hidden-tests/``, ``answers/``).
    """
    targets: list[tuple[str, bool]] = []
    for line in _placement_lines(manifest):
        if not line.lstrip().startswith("|"):
            continue
        cells = line.strip().strip("|").split("|")
        if all(re.fullmatch(r"[\s:\-]*", cell) for cell in cells):
            continue
        creating = bool(_NEW_FILE.search(line))
        for candidate in _PATH_TOKEN.findall(line.replace("`", " ")):
            if not _HAS_EXTENSION.search(candidate):
                continue
            relative = _resolve_target(candidate, stage, creating)
            if relative is not None and all(relative != known for known, _ in targets):
                targets.append((relative, creating and not (stage / relative).exists()))
    return targets


def _inject(path: Path, token: str) -> str | None:
    """Write the token into an existing file; the placement kind, or None if it cannot carry it."""
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(content)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        path.write_text(
            json.dumps({"_build": token, **data}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return "sample_data"
    if suffix in _HTML_SUFFIXES:
        path.write_text(f"{content.rstrip(chr(10))}\n<!-- canary: {token} -->\n", encoding="utf-8")
        return "comment"
    style = "//" if suffix in _SLASH_SUFFIXES else "#"
    if content.startswith("#!"):
        first, _, rest = content.partition("\n")
        path.write_text(f"{first}\n{inject_comment(rest, token, style=style)}", encoding="utf-8")
    else:
        path.write_text(inject_comment(content, token, style=style), encoding="utf-8")
    return "comment"


def _entry_candidates(stage: Path, entry_files: Sequence[str]) -> list[str]:
    """Entry files first, then every other file in a stable order."""
    ordered = [
        entry for entry in entry_files if not entry.endswith("/") and (stage / entry).is_file()
    ]
    rest = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())
    return [*ordered, *(path for path in rest if path not in ordered)]


def stage_workspace(
    *,
    pack: TaskPack,
    workspace: WorkspacePaths,
    trial_id: str,
    token: str,
    scratch: Path,
) -> StagedWorkspace:
    """Copy the starter files, write the canary into them and hash every placement.

    Raises ``CanaryInjectionError`` when no workspace file can carry the token (S3 fails).
    """
    stage = scratch / "workspace"
    if workspace.starter_dir is not None and workspace.starter_dir.is_dir():
        shutil.copytree(workspace.starter_dir, stage, symlinks=True)
    else:
        stage.mkdir(parents=True)
    manifest_file = pack.file("canary.md")
    manifest = manifest_file.read_text(encoding="utf-8") if manifest_file.is_file() else ""

    placed: list[tuple[str, str]] = []
    for relative, create in parse_canary_targets(manifest, stage):
        target = stage / relative
        if create:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(inject_comment("", token), encoding="utf-8")
            placed.append((relative, "file"))
        elif (kind := _inject(target, token)) is not None:
            placed.append((relative, kind))
    if not placed:
        # No machine-readable placement: fall back to a marker line in the first entry file.
        for relative in _entry_candidates(stage, workspace.entry_files):
            if (kind := _inject(stage / relative, token)) is not None:
                placed.append((relative, kind))
                break
    if not placed:
        raise CanaryInjectionError(f"no workspace file of {pack.task_id} can carry the canary")

    hashes = {rel: hashlib.sha256((stage / rel).read_bytes()).hexdigest() for rel, _ in placed}
    placements = tuple(
        CanaryPlacement(trial_id=trial_id, path=rel, kind=kind, token=token) for rel, kind in placed
    )
    # build_placements validates the token format once more for the whole set.
    build_placements(trial_id, token, [rel for rel, _ in placed])
    instruction_file = pack.file("instruction.md")
    instruction = (
        instruction_file.read_text(encoding="utf-8").replace("{CNRY}", token)
        if instruction_file.is_file()
        else ""
    )
    return StagedWorkspace(stage, placements, hashes, instruction)


async def upload_and_verify(
    sandbox: Sandbox, staged: StagedWorkspace, remote_root: str, scratch: Path
) -> None:
    """Upload the staged workspace and check each canary file against its recorded hash (I9)."""
    await sandbox.upload(staged.directory, remote_root)
    for relative, expected in staged.hashes.items():
        copy = scratch / "readback" / relative
        await sandbox.download(f"{remote_root.rstrip('/')}/{relative}", copy)
        actual = hashlib.sha256(await asyncio.to_thread(copy.read_bytes)).hexdigest()
        if actual != expected:
            raise CanaryInjectionError(
                f"canary placement {relative} differs in the sandbox", detail={"path": relative}
            )


# -- S4: identity hygiene -----------------------------------------------------------------


async def _stdout(sandbox: Sandbox, argv: list[str]) -> str:
    result = await sandbox.exec(argv, timeout_s=PROBE_TIMEOUT_S)
    if result.exit_code != 0 or result.timed_out:
        raise IdentityHygieneError(f"cannot read sandbox identity: {' '.join(argv)} failed")
    return result.stdout.decode("utf-8", errors="replace").strip()


async def check_identity_hygiene(
    sandbox: Sandbox, *, identifiers: Sequence[str], workspace_root: str
) -> None:
    """The agent must not see who evaluates it: neutral user, hostname, environment, workspace.

    ``identifiers`` are the evaluation identifiers (trial, task, model, batch) that must not
    appear anywhere the agent can look. Raises ``IdentityHygieneError`` naming each violation.
    """
    needles = {item.lower() for item in identifiers if len(item) >= _MIN_IDENTIFIER_LEN}
    problems: list[str] = []

    def mentions(value: str) -> bool:
        lowered = value.lower()
        return any(needle in lowered for needle in needles)

    for label, value in (
        ("user", await _stdout(sandbox, ["id", "-un"])),
        ("hostname", await _stdout(sandbox, ["hostname"])),
        ("workspace path", workspace_root),
    ):
        if mentions(value):
            problems.append(f"{label} carries an evaluation identifier")
    for line in (await _stdout(sandbox, ["env"])).splitlines():
        name, _, value = line.partition("=")
        if _EVAL_ENV_NAME.match(name) or mentions(name) or mentions(value):
            problems.append(f"environment variable {name} carries an evaluation identifier")
        elif _CREDENTIAL.search(value):
            problems.append(f"environment variable {name} holds a credential")
    if problems:
        raise IdentityHygieneError("; ".join(problems), detail={"violations": problems})


# -- S5: isolation preflight ----------------------------------------------------------------


async def run_isolation_probes(sandbox: Sandbox, profile: IsolationProfile) -> list[ProbeResult]:
    """Run each non-waived probe of I1-I9 in the sandbox; exit code 0 is a pass."""
    results: list[ProbeResult] = []
    for invariant in PREFLIGHT_INVARIANTS:
        entry = profile.invariants.get(invariant)
        if entry is None or entry.waived or not entry.probe:
            continue
        outcome = await sandbox.exec(entry.probe, timeout_s=PROBE_TIMEOUT_S)
        results.append(ProbeResult(invariant, entry.probe, outcome.exit_code, outcome.timed_out))
    return results
