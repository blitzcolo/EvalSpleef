"""End-to-end harness fixtures: real spleef CLI, real gateway, real local Docker.

The scenario drives the actual ``spleef`` console script in subprocesses with the
repository environment. It fails loudly (never skips) when Docker is unreachable
or an image is missing, and saves every CLI transcript, the event logs, the batch
report and the gateway usage records under ``.omo/reports/e2e/``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from evalspleef.testing.fake_openai import FakeOpenAIServer

REPO = Path(__file__).resolve().parents[2]
REPORTS = REPO / ".omo" / "reports" / "e2e"
TASK_IDS = ("logic-001", "data-001", "debug-001")
IMAGES = tuple(f"llm-eval/{task}:local" for task in TASK_IDS)
SPLEEF_BIN = Path(sys.executable).with_name("spleef")
GATEWAY_BIN = Path(sys.executable).with_name("spleef-gateway")
GATEWAY_TOKEN = "e2e-admin-token"
FAKE_API_KEY = "e2e-fake-key"


# -- marker: e2e never runs in the default unit pass ---------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: end-to-end batch on real local Docker")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    marker_expr = config.getoption("-m", default="") or ""
    if "e2e" in str(marker_expr):
        return
    skip = pytest.mark.skip(reason="e2e tests run only with -m e2e (they need Docker)")
    for item in items:
        if item.get_closest_marker("e2e") is not None:
            item.add_marker(skip)


# -- docker prerequisite ------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_ready() -> None:
    """Hard precondition: a reachable daemon and the three task images."""
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=30)
    except OSError as exc:
        pytest.fail(f"the 'docker' CLI is not on PATH ({exc}); the e2e suite needs Docker")
    if info.returncode != 0:
        pytest.fail(
            "the Docker daemon is not reachable; the e2e suite runs real containers and "
            "must never be skipped silently. Start Docker Desktop/colima/orbstack. "
            f"daemon said: {info.stderr.strip()[:300]}"
        )
    missing = [
        image
        for image in IMAGES
        if subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=30
        ).returncode
        != 0
    ]
    if missing:
        pytest.fail(
            f"missing Docker image(s): {', '.join(missing)}; build them from templates/ "
            "on this host before running the e2e suite"
        )


# -- scripted fake model ------------------------------------------------------------


@dataclass(frozen=True)
class AgentScript:
    """What the scripted fake model does for one task's trial."""

    marker: str  # substring of the (rendered) instruction that identifies the task
    command: str  # bash heredoc implementing the pack's reference solution A
    final_message: str


def _build_ref_scripts() -> dict[str, AgentScript]:
    """Reference solution A per pack, as a bash heredoc the agent's bash tool runs.

    The command computes its content from the pack's ``answers/`` directory (never
    from anything inside the sandbox). For logic-001 and data-001 it also removes
    the canary marker the harness injected into a hash-locked input file (upstream
    defect: canary injection defeats the pack's own HG3/oracle); see the report.
    """
    scripts: dict[str, AgentScript] = {}
    for task_id in TASK_IDS:
        pack = REPO / "tasks" / task_id
        instruction = (pack / "instruction.md").read_text(encoding="utf-8")
        heading = next(
            line.lstrip("#").strip() for line in instruction.splitlines() if line.strip()
        )
        if task_id == "logic-001":
            import tempfile

            with tempfile.TemporaryDirectory() as scratch:
                answer = Path(scratch) / "answer.json"
                subprocess.run(
                    [
                        "python3",
                        str(pack / "answers" / "reference-a.py"),
                        str(pack / "workspace" / "instances.json"),
                        str(answer),
                    ],
                    check=True,
                )
                answer_text = answer.read_text(encoding="utf-8").rstrip("\n")
            command = "\n".join(
                [
                    "cat > /workspace/answer.json <<'SPL_EOF'",
                    answer_text,
                    "SPL_EOF",
                ]
            )
        elif task_id == "data-001":
            reference = (pack / "answers" / "reference-b.py").read_text(encoding="utf-8")
            command = "\n".join(
                [
                    "cat > /workspace/clean_logs.py <<'SPL_EOF'",
                    reference.rstrip("\n"),
                    "SPL_EOF",
                ]
            )
        else:  # debug-001
            patch_text = (pack / "answers" / "reference-a.patch").read_text(encoding="utf-8")
            command = "\n".join(
                [
                    "cd /workspace",
                    "patch -p1 <<'SPL_EOF'",
                    patch_text.rstrip("\n"),
                    "SPL_EOF",
                ]
            )
        scripts[task_id] = AgentScript(
            marker=heading,
            command=command,
            final_message="Reference solution applied.",
        )
    return scripts


def _make_server(scripts: Mapping[str, AgentScript]) -> FakeOpenAIServer:
    from evalspleef.testing.fake_openai import _message_text, _messages

    class ScriptedFakeOpenAI(FakeOpenAIServer):
        """One server, three models: scripted in trials, rule-based for probes."""

        MODELS = ("fake-ref-agent", "fake-empty-agent", "fake-judge")

        SCRIPTED = frozenset({"fake-ref-agent", "fake-empty-agent"})

        def __init__(self) -> None:
            super().__init__(model="fake-ref-agent", api_key=FAKE_API_KEY, parallel_tool_calls=True)
            self._scripts = dict(scripts)
            self._current_model = ""

        async def _models(self, request: Request) -> Response:
            del request
            return JSONResponse(
                {
                    "object": "list",
                    "data": [
                        {"id": model, "object": "model", "created": 0, "owned_by": "evalspleef"}
                        for model in self.MODELS
                    ],
                }
            )

        async def _chat(self, request: Request) -> Response:
            body = await request.json()
            self._current_model = str(body.get("model") or "")
            return await super()._chat(request)

        def _envelope(self, *, chunk: bool) -> dict[str, object]:
            payload = super()._envelope(chunk=chunk)
            # identity (P-14): the response model must be the requested model
            payload["model"] = self._current_model or self.reported_model
            return payload

        def _trial_script(self, body: Mapping[str, object]) -> AgentScript | None:
            """The task script when the request is a trial of a scripted agent model."""
            if self._current_model not in self.SCRIPTED:
                return None
            for script in self._scripts.values():
                if any(
                    script.marker in _message_text(message)
                    for message in _messages(body)
                    if message.get("role") == "user"
                ):
                    return script
            return None

        def _generate(self, body: Mapping[str, object]) -> tuple[dict[str, object], str, int]:
            from evalspleef.testing.fake_openai import count_tokens

            messages = _messages(body)
            if self._current_model == "fake-judge" and any(
                "rubric_item_id" in _message_text(message) for message in messages
            ):
                # a judge panel sample: answer the binary verdict question
                text = '{"verdict": "pass"}'
                return {"role": "assistant", "content": text}, "stop", count_tokens(text)
            script = self._trial_script(body)
            if script is None:
                return super()._generate(body)
            if self._current_model == "fake-empty-agent":
                # the empty agent finishes immediately without tool calls
                text = "There is nothing for me to do."
                return {"role": "assistant", "content": text}, "stop", count_tokens(text)
            if any(message.get("role") == "tool" for message in messages):
                text = script.final_message
                return {"role": "assistant", "content": text}, "stop", count_tokens(text)
            arguments = json.dumps({"command": script.command, "timeout_s": 300})
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_0",
                        "type": "function",
                        "function": {"name": "bash", "arguments": arguments},
                    }
                ],
            }
            return message, "tool_calls", count_tokens(arguments)

    return ScriptedFakeOpenAI()


@pytest.fixture(scope="session")
def ref_scripts() -> Mapping[str, AgentScript]:
    return _build_ref_scripts()


@pytest.fixture(scope="session")
def fake_server(ref_scripts: Mapping[str, AgentScript]) -> Iterator[object]:
    server = _make_server(ref_scripts)
    server.start()
    yield server
    server.stop()


# -- gateway ------------------------------------------------------------------------


@dataclass(frozen=True)
class Gateway:
    data_url: str
    admin_url: str
    token: str
    process: subprocess.Popen[str]

    def admin_json(self, path: str) -> object:
        request = urllib.request.Request(
            f"{self.admin_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(gateway: Gateway, log_path: Path) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if gateway.process.poll() is not None:
            log = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            pytest.fail(f"the gateway process exited early ({gateway.process.returncode}):\n{log}")
        data_ok = False
        admin_ok = False
        try:
            with urllib.request.urlopen(f"{gateway.data_url}/healthz", timeout=2) as response:
                data_ok = response.status == 200
        except OSError:
            pass
        try:
            gateway.admin_json("/admin/version")
            admin_ok = True
        except OSError:
            pass
        if data_ok and admin_ok:
            return
        time.sleep(0.1)
    log = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    pytest.fail(f"the gateway did not become ready within 60s:\n{log}")


@pytest.fixture(scope="session")
def gateway(tmp_path_factory: pytest.TempPathFactory, docker_ready: None) -> Iterator[Gateway]:
    del docker_ready  # ordering only: report the Docker problem before anything else
    if not GATEWAY_BIN.is_file():
        pytest.fail(f"the spleef-gateway executable is missing: {GATEWAY_BIN}")
    work = tmp_path_factory.mktemp("gateway")
    log_path = work / "gateway.log"
    data_port, admin_port = _free_port(), _free_port()
    env = os.environ | {
        "SPLEEF_GATEWAY_ADMIN_TOKEN": GATEWAY_TOKEN,
        "SPLEEF_GATEWAY_DATA_PORT": str(data_port),
        "SPLEEF_GATEWAY_ADMIN_PORT": str(admin_port),
        "SPLEEF_GATEWAY_STORE": f"sqlite:///{work / 'gateway.sqlite3'}",
        # the gateway resolves env:// upstream credentials in its own process
        "SPLEEF_FAKE_API_KEY": FAKE_API_KEY,
    }
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(GATEWAY_BIN)],
        env=env,
        cwd=REPO,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    running = Gateway(
        data_url=f"http://127.0.0.1:{data_port}",
        admin_url=f"http://127.0.0.1:{admin_port}",
        token=GATEWAY_TOKEN,
        process=process,
    )
    try:
        _wait_ready(running, log_path)
        yield running
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
        log_handle.close()
        (REPORTS / "gateway").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(log_path, REPORTS / "gateway" / "gateway.log")


# -- the CLI environment -------------------------------------------------------------


@dataclass
class E2E:
    env: dict[str, str]
    work: Path
    store_path: Path
    events_dir: Path
    gateway: Gateway
    server: object
    config_dir: Path
    counter: int = 0

    def run_cli(self, name: str, *argv: str) -> subprocess.CompletedProcess[str]:
        """Run the real spleef CLI and save the full transcript under reports/."""
        self.counter += 1
        result = subprocess.run(
            [str(SPLEEF_BIN), *argv],
            env=self.env,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        transcript = REPORTS / "cli" / f"{self.counter:02d}-{name}.txt"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            f"$ spleef {' '.join(argv)}\n"
            f"exit={result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}\n",
            encoding="utf-8",
        )
        return result

    def options(self, *extra: str, with_batch: bool = True) -> list[str]:
        options = [
            "--models",
            str(self.config_dir / "models.yaml"),
            "--providers",
            str(self.config_dir / "providers.yaml"),
            "--tasks-dir",
            str(REPO / "tasks"),
            "--templates-registry",
            str(REPO / "templates" / "registry.json"),
        ]
        if with_batch:
            options += ["--batch", str(self.config_dir / "batch.yaml")]
        return [*options, *extra]


@pytest.fixture(scope="session")
def e2e(
    tmp_path_factory: pytest.TempPathFactory,
    gateway: Gateway,
    fake_server: object,
    docker_ready: None,
) -> Iterator[E2E]:
    del docker_ready
    if not SPLEEF_BIN.is_file():
        pytest.fail(f"the spleef executable is missing: {SPLEEF_BIN}")
    shutil.rmtree(REPORTS, ignore_errors=True)
    (REPORTS / "cli").mkdir(parents=True, exist_ok=True)
    (REPORTS / "events").mkdir(parents=True, exist_ok=True)
    (REPORTS / "gateway").mkdir(parents=True, exist_ok=True)
    (REPORTS / "tmp").mkdir(parents=True, exist_ok=True)
    work = tmp_path_factory.mktemp("e2e")
    store_path = work / "store.sqlite3"
    events_dir = REPORTS / "events"
    # The pristine example configuration, verbatim.
    config_dir = work / "config"
    config_dir.mkdir()
    for name in ("models.yaml", "providers.yaml", "batch.yaml"):
        shutil.copyfile(REPO / "config" / "examples" / name, config_dir / name)
    # One deviation: the example routes fall back to mock-sandbox once local-docker is at
    # max_concurrency; this scenario must run every lease on real Docker, so the routes
    # keep local-docker only and the scheduler queues instead of overflowing.
    providers_file = config_dir / "providers.yaml"
    providers = yaml.safe_load(providers_file.read_text(encoding="utf-8"))
    for route in providers["routes"]:
        route["prefer"] = [name for name in route["prefer"] if name == "local-docker"]
    providers_file.write_text(yaml.safe_dump(providers, sort_keys=False), encoding="utf-8")
    env = os.environ | {
        "SPLEEF_STORE_URL": f"sqlite:///{store_path}",
        "SPLEEF_EVENTS_URL": f"jsonl://{events_dir}",
        "SPLEEF_STATE_DIR": str(work / "state"),
        "SPLEEF_GATEWAY_ADMIN_URL": gateway.admin_url,
        "SPLEEF_GATEWAY_ADMIN_TOKEN": GATEWAY_TOKEN,
        "SPLEEF_GATEWAY_URL": gateway.data_url,
        "SPLEEF_FAKE_BASE_URL": fake_server.base_url,  # type: ignore[attr-defined]
        "SPLEEF_FAKE_API_KEY": FAKE_API_KEY,
        "TMPDIR": str(REPORTS / "tmp"),  # runner transcripts land in the report dir
    }
    yield E2E(
        env=env,
        work=work,
        store_path=store_path,
        events_dir=events_dir,
        gateway=gateway,
        server=fake_server,
        config_dir=config_dir,
    )
