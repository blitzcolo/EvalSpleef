"""Shared fixture-pack builder for the taskpack tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

PackBuilder = Callable[[str], Path]

TASK_YAML = """\
id: test-001
version: 1.0.0
track: logic
title: fixture task
instruction: instruction.md
persona: persona.md
rubric_ref: rubric.yaml
defects_ref: defects.yaml
workspace:
  mount: /workspace
  starter: workspace/
runtime:
  requirements:
    os: linux
    arch: [x86_64, arm64]
    min_isolation: L1
    network: "off"
    gpu: false
    gui: false
    cpus: 2
    memory_mb: 2048
    disk_mb: 1024
    max_lifetime_s: 7200
  environment:
    oci_image: "llm-eval/test@sha256:abc123"
    macos_image: null
    windows_image: null
  execution_modes: [remote-tools]
  model_requirements:
    tool_calling: required
    min_context_tokens: 32000
budget:
  max_rounds: 6
  max_wall_clock_s: 3600
  max_observe_s: 600
hard_gates:
  - id: HG1
    kind: script
    script: hidden-tests/run.sh
"""

RUBRIC_YAML = """\
schema_version: "1.0"
task_id: test-001
rubric_version: 1.0.0
items:
  - id: R1
    kind: llm_judge
    severity: functional
    description: the answer is correct
    f1_symptom: the answer looks wrong
  - id: R2
    kind: script
    severity: minor
    description: code is tidy
    f1_symptom: the code is a mess
"""

DEFECTS_YAML = """\
document_type: catalog
schema_version: "1.0"
task_id: test-001
catalog_version: 1.0.0
items:
  - rubric_item_id: R1
    title: wrong answer
    severity: functional
    f1_symptom: the answer looks wrong
    detect: {source: agent_judge, check_ids: [tests.t1]}
  - rubric_item_id: R2
    title: messy code
    severity: minor
    f1_symptom: the code is a mess
"""

TEXT_FILES = {
    "instruction.md": "Do the thing.\n",
    "persona.md": "A user.\n",
    "canary.md": "Canary locations.\n",
    "notes.md": "Notes.\n",
    "answers/reference-a.py": "print('a')\n",
    "answers/reference-b.py": "print('b')\n",
    "hidden-tests/run.sh": "#!/bin/sh\nexit 0\n",
}


def build_pack(root: Path, task_yaml: str = TASK_YAML, pack_id: str = "test-001") -> Path:
    """Materialise a minimal but contract-valid fixture pack under ``root``."""
    pack = root / pack_id
    pack.mkdir(parents=True)
    (pack / "workspace").mkdir()
    (pack / "task.yaml").write_text(task_yaml, encoding="utf-8")
    (pack / "rubric.yaml").write_text(RUBRIC_YAML, encoding="utf-8")
    (pack / "defects.yaml").write_text(DEFECTS_YAML, encoding="utf-8")
    for relative, content in TEXT_FILES.items():
        path = pack / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return pack


@pytest.fixture
def pack_factory(tmp_path: Path) -> Iterator[PackBuilder]:
    """Return a builder bound to a fresh tmp dir; each call makes one pack."""
    counter = 0

    def build(task_yaml: str = TASK_YAML) -> Path:
        nonlocal counter
        pack_id = "test-001" if counter == 0 else f"test-001-{counter}"
        counter += 1
        return build_pack(tmp_path, task_yaml, pack_id)

    yield build


@pytest.fixture
def task_yaml_template() -> str:
    return TASK_YAML


@pytest.fixture
def rubric_yaml_template() -> str:
    return RUBRIC_YAML


@pytest.fixture
def defects_yaml_template() -> str:
    return DEFECTS_YAML
