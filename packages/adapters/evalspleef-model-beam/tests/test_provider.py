from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence

import pytest
from evalspleef_model_beam.conformance import (
    HF_TOKEN_REF,
    IMAGE,
    TOKEN,
    TOKEN_REF,
    FakeBeamGateway,
    FakeBeamSdk,
    FakeLauncher,
)
from evalspleef_model_beam.errors import (
    CredentialRejected,
    DeploymentNotFound,
    LaunchFailed,
    ParallelismMismatch,
    StubIdMissing,
)
from evalspleef_model_beam.launcher import RESULT_PREFIX
from evalspleef_model_beam.provider import BeamPodProvider, deployment_name, parse_launch_result

from evalspleef.spi.model import (
    DEPLOYMENT_ID_KEY,
    EndpointHandle,
    EndpointState,
    ManagementMode,
    ModelSpec,
)
from evalspleef.testing.secrets import DictSecretResolver

HF_SECRET = "hf_secret_value_do_not_leak"
SHA = "1a2b3c4d" * 5
SLUG = "qwen38-27b"
REPO = "evalspleef-conformance/public-model"
WEIGHTS_DIR = f"/huggingface_models/{REPO}"
VOLUME = {"weights_volume": "huggingface_models"}

SpecFactory = Callable[..., ModelSpec]


async def deployed(provider: BeamPodProvider, spec: ModelSpec) -> EndpointHandle:
    return await provider.deploy(spec, await provider.plan(spec))


def test_is_api_mode() -> None:
    assert BeamPodProvider.management_mode is ManagementMode.API


# -- validate --------------------------------------------------------------------------------


async def test_validate_accepts_a_pinned_public_model(
    provider: BeamPodProvider, make_spec: SpecFactory
) -> None:
    report = await provider.validate(make_spec())
    failed = [check for check in report.checks if not check[1]]
    assert [check_id for check_id, _ok, _detail in failed] in ([], ["launcher"]), failed


async def test_validate_names_the_unpinned_image_the_missing_gpu_and_the_hf_token(
    gateway: FakeBeamGateway, fake_launcher: FakeLauncher, make_spec: SpecFactory
) -> None:
    provider = BeamPodProvider(
        "acme",
        {"api_key_ref": TOKEN_REF, "image": "lmsysorg/sglang:latest"},
        DictSecretResolver({TOKEN_REF: TOKEN}),
        launch_runner=fake_launcher,
        api_transport=gateway.transport(),
    )
    report = await provider.validate(make_spec(access="gated", token_ref=HF_TOKEN_REF, gpu=None))
    failed = {check_id for check_id, ok, _detail in report.checks if not ok}
    assert {"image_digest_pinned", "gpu", "hf_token"} <= failed
    assert not report.ok


async def test_validate_passes_without_an_injected_hf_checker(
    gateway: FakeBeamGateway, fake_launcher: FakeLauncher, make_spec: SpecFactory
) -> None:
    # The endpoint manager runs the HF preflight before validate and never assigns
    # hf_checker; a provider built the way the control plane builds it must still validate.
    provider = BeamPodProvider(
        "acme",
        {"api_key_ref": TOKEN_REF, "image": IMAGE},
        DictSecretResolver({TOKEN_REF: TOKEN, HF_TOKEN_REF: HF_SECRET}),
        launch_runner=fake_launcher,
        api_transport=gateway.transport(),
    )
    assert provider.hf_checker is None
    report = await provider.validate(make_spec(access="gated", token_ref=HF_TOKEN_REF))
    failed = [check_id for check_id, ok, _detail in report.checks if not ok]
    assert failed in ([], ["launcher"]), failed


# -- deploy --------------------------------------------------------------------------------------


async def test_deploy_keeps_the_token_out_of_argv_and_the_record(
    provider: BeamPodProvider,
    fake_launcher: FakeLauncher,
    sdk: FakeBeamSdk,
    make_spec: SpecFactory,
) -> None:
    handle = await deployed(provider, make_spec(access="gated", token_ref=HF_TOKEN_REF))
    call = fake_launcher.calls[0]
    assert call["env"]["BEAM_TOKEN"] == TOKEN
    assert call["env"]["CONFIG_PATH"].startswith(call["cwd"])
    assert all(TOKEN not in arg and HF_SECRET not in arg for arg in call["argv"])
    assert call["request"]["env"] == {"HF_TOKEN": HF_SECRET}
    assert handle.observed["deploy_request"]["env"] == {"HF_TOKEN": "***"}
    assert HF_SECRET not in repr(handle.observed) and TOKEN not in repr(handle.observed)
    pod = sdk.pods[0]
    assert pod["authorized"] is True
    assert pod["image"] == ("registry", IMAGE)
    assert pod["gpu"] == "RTX5090" and pod["gpu_count"] == 1
    entrypoint = pod["entrypoint"]
    assert entrypoint[:3] == ["python3", "-m", "sglang.launch_server"]
    assert entrypoint[entrypoint.index("--revision") + 1] == SHA
    assert entrypoint[entrypoint.index("--served-model-name") + 1] == SLUG
    assert entrypoint[entrypoint.index("--context-length") + 1] == "32768"


async def test_deploy_appends_engine_extra_args(
    provider: BeamPodProvider, sdk: FakeBeamSdk, make_spec: SpecFactory
) -> None:
    extra = ["--reasoning-parser", "qwen3", "--tool-call-parser", "qwen3_coder"]
    await deployed(provider, make_spec(engine_extra_args=extra, context_length=16384))
    entrypoint = sdk.pods[0]["entrypoint"]
    assert entrypoint[-4:] == extra
    assert entrypoint[entrypoint.index("--context-length") + 1] == "16384"


async def test_deploy_scales_to_the_minimum_and_ends_ready(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    deployment_id = handle.external_ids["beam_deployment_id"]
    assert handle.external_ids[DEPLOYMENT_ID_KEY] == deployment_id
    assert ("POST", f"/deployments/{deployment_id}/scale", {"containers": 1}) in gateway.calls
    assert handle.state is EndpointState.READY
    assert handle.base_url == gateway.inference_url + "/v1"
    assert handle.observed["beam_deployment_name"] == deployment_name(handle.model)
    route = provider.upstream(handle)
    assert (route.api_base, route.model) == (handle.base_url, SLUG)
    assert route.default_params == {}


async def test_the_route_carries_the_specs_default_params(
    provider: BeamPodProvider, make_spec: SpecFactory
) -> None:
    # The gateway applies these to every request, e.g. a model's thinking mode.
    defaults = {"chat_template_kwargs": {"enable_thinking": False}}
    handle = await deployed(provider, make_spec(default_params=defaults))
    assert provider.upstream(handle).default_params == defaults


async def test_a_failed_launch_removes_the_deployment_it_left_behind(
    provider: BeamPodProvider, gateway: FakeBeamGateway, sdk: FakeBeamSdk, make_spec: SpecFactory
) -> None:
    spec = make_spec()
    stale = gateway.register(deployment_name(spec))
    sdk.fail_with = "no capacity for RTX5090"
    with pytest.raises(LaunchFailed) as caught:
        await deployed(provider, spec)
    assert "no capacity for RTX5090" in str(caught.value)
    assert caught.value.detail["removed"] == [stale["id"]]
    assert gateway.deployments == {}


async def test_a_launcher_without_a_result_fails_with_a_scrubbed_stderr_tail(
    gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    async def crashing(
        argv: Sequence[str], stdin: bytes, env: Mapping[str, str], cwd: str, timeout_s: float
    ) -> tuple[int, bytes, bytes]:
        return 1, b"Traceback ...\n", f"grpc failed for token {TOKEN}\n".encode()

    provider = BeamPodProvider(
        "acme",
        {"api_key_ref": TOKEN_REF, "image": IMAGE},
        DictSecretResolver({TOKEN_REF: TOKEN}),
        launch_runner=crashing,
        api_transport=gateway.transport(),
    )
    with pytest.raises(LaunchFailed) as caught:
        await deployed(provider, make_spec())
    assert "without a result" in str(caught.value)
    assert TOKEN not in caught.value.detail["stderr_tail"]
    assert "***" in caught.value.detail["stderr_tail"]


def test_parse_launch_result_takes_the_last_result_line() -> None:
    stdout = (
        f'{RESULT_PREFIX}{{"ok": false}}\n=> Deploying\n{RESULT_PREFIX}{{"ok": true}}\n'
    ).encode()
    assert parse_launch_result(stdout) == {"ok": True}
    assert parse_launch_result(b"=> no result here\n") is None


# -- status ------------------------------------------------------------------------------------


async def test_status_is_deploying_until_a_container_runs_and_the_engine_serves(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    gateway.container_status = "PENDING"
    assert await provider.status(handle) is EndpointState.DEPLOYING
    gateway.container_status = "RUNNING"
    url = handle.base_url
    handle.base_url = "http://127.0.0.1:9/v1"  # nothing listens: the engine is still loading
    assert await provider.status(handle) is EndpointState.DEPLOYING
    handle.base_url = url
    assert await provider.status(handle) is EndpointState.READY


async def test_status_of_a_deployment_removed_elsewhere_is_failed(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    gateway.deployments.clear()
    assert await provider.status(handle) is EndpointState.FAILED


async def test_gateway_rejections_map_to_typed_errors(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    gateway.token = "rotated"
    with pytest.raises(CredentialRejected):
        await provider.status(handle)
    gateway.token = TOKEN
    handle.external_ids["beam_deployment_id"] = "dep-unknown"
    with pytest.raises(DeploymentNotFound):
        await provider.scale(handle, 1, 1)


# -- scale / sleep / wake / terminate ------------------------------------------------------------


async def test_scale_provisions_the_upper_bound(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    await provider.scale(handle, 1, 2)
    assert gateway.containers[handle.external_ids["beam_deployment_id"]] == 2


async def test_sleep_scales_to_zero_and_wake_brings_one_container_back(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    deployment_id = handle.external_ids["beam_deployment_id"]
    await provider.teardown(handle, "sleep")
    assert gateway.containers[deployment_id] == 0
    assert await provider.status(handle) is EndpointState.SLEEPING
    assert await provider.wake(handle, timeout_s=60) is EndpointState.WARM
    assert gateway.containers[deployment_id] == 1


async def test_terminate_stops_and_deletes_and_is_idempotent(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    deployment_id = handle.external_ids["beam_deployment_id"]
    await provider.teardown(handle, "terminate")
    assert ("POST", f"/deployments/{deployment_id}/stop", None) in gateway.calls
    assert ("DELETE", f"/deployments/{deployment_id}", None) in gateway.calls
    assert gateway.deployments == {}
    assert await provider.status(handle) is EndpointState.RETIRED
    await provider.teardown(handle, "terminate")
    assert handle.state is EndpointState.RETIRED


# -- engine log ----------------------------------------------------------------------------------

T0 = 1_800_000_000_000_000_000
"""Nanosecond epoch the fake log entries start from."""


def _stub_of(handle: EndpointHandle) -> str:
    return handle.external_ids["beam_stub_id"]


async def test_engine_log_holds_only_this_stubs_lines_without_the_beam_token(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    gateway.add_log_lines("stub-other", ["other pod"], start_ns=T0 + 500_000)
    stamps = gateway.add_log_lines(
        _stub_of(handle), ["sglang up", f"Authorization: Bearer {TOKEN}"], start_ns=T0
    )

    log = await provider.engine_log(handle, max_bytes=10_000)

    assert log.text == f"{stamps[0]} sglang up\n{stamps[1]} Authorization: Bearer ***"
    assert (log.truncated, log.dropped) == (False, 0)
    first = gateway.log_queries[0]
    assert (first["stub_id"], first["limit"]) == (_stub_of(handle), "1000")
    assert not {"deployment_id", "tail"} & first.keys()  # the vendor ignores both


async def test_engine_log_drops_and_counts_entries_of_other_stubs_the_api_let_through(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    gateway.ignores_stub_filter = True
    gateway.add_log_lines("stub-other", ["other pod"], start_ns=T0)
    gateway.add_log_lines(_stub_of(handle), ["mine"], start_ns=T0 + 1_000_000)

    log = await provider.engine_log(handle, max_bytes=10_000)

    assert log.text.endswith(" mine") and "other pod" not in log.text
    assert log.dropped == 1


async def test_engine_log_pages_back_past_the_1000_row_limit_and_keeps_every_line(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    messages = [f"line {index}" for index in range(2500)]
    gateway.add_log_lines("stub-other", ["noise"] * 40, start_ns=T0)
    gateway.add_log_lines(_stub_of(handle), messages, start_ns=T0)

    log = await provider.engine_log(handle, max_bytes=10_000_000)

    assert [line.split(" ", 1)[1] for line in log.text.splitlines()] == messages
    assert (log.truncated, log.dropped) == (False, 0)
    assert [query["limit"] for query in gateway.log_queries] == ["1000"] * 4  # 1000+1000+500+empty


async def test_engine_log_stops_when_the_api_ignores_end_time_and_says_so(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    gateway.ignores_end_time = True
    gateway.add_log_lines(_stub_of(handle), [f"line {index}" for index in range(1500)], start_ns=T0)

    log = await provider.engine_log(handle, max_bytes=10_000_000)

    assert len(log.text.splitlines()) == 1000  # the newest page, once
    assert log.truncated is True
    assert len(gateway.log_queries) <= 2  # a stalled cursor must not loop


async def test_engine_log_keeps_the_newest_bytes_and_flags_the_cut(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    gateway.add_log_lines(_stub_of(handle), ["old line", "newest line"], start_ns=T0)

    log = await provider.engine_log(handle, max_bytes=11)

    assert (log.text, log.truncated) == ("newest line", True)


async def test_engine_log_without_a_stub_id_sends_no_request(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    handle.external_ids["beam_stub_id"] = ""
    gateway.add_log_lines("stub-other", ["other pod"], start_ns=T0)

    with pytest.raises(StubIdMissing):
        await provider.engine_log(handle, max_bytes=100)

    assert gateway.log_queries == []


# -- weights from a Beam Volume ------------------------------------------------------------------


async def test_volume_weights_are_fetched_on_cpu_before_the_gpu_pod_loads_them(
    provider: BeamPodProvider,
    fake_launcher: FakeLauncher,
    sdk: FakeBeamSdk,
    make_spec: SpecFactory,
) -> None:
    spec = make_spec(
        access="gated", token_ref=HF_TOKEN_REF, hf_token_secret="HUGGINGFACE_TOKEN", **VOLUME
    )
    handle = await deployed(provider, spec)
    fetch, deploy = (call["request"] for call in fake_launcher.calls)
    assert [fetch["action"], deploy["action"]] == ["fetch", "deploy"]
    mount = [{"name": "huggingface_models", "mount_path": "/huggingface_models"}]
    assert fetch["volumes"] == deploy["volumes"] == mount
    assert (fetch["gpu"], fetch["gpu_count"], fetch["ports"]) == ("", 0, [])
    assert fetch["env"] == {
        "SPLEEF_REPO": REPO,
        "SPLEEF_REVISION": SHA,
        "SPLEEF_DIR": WEIGHTS_DIR,
        "SPLEEF_TOKEN_ENV": "HUGGINGFACE_TOKEN",
    }
    assert fetch["secrets"] == ["HUGGINGFACE_TOKEN"]
    assert HF_SECRET not in json.dumps([fetch, deploy])
    entrypoint = deploy["entrypoint"]
    assert entrypoint[entrypoint.index("--model-path") + 1] == WEIGHTS_DIR
    assert "--revision" not in entrypoint
    assert deploy["env"] == {}
    assert sdk.pods[1]["volumes"] == [
        sdk.Volume(name="huggingface_models", mount_path="/huggingface_models")
    ]
    assert handle.observed["fetch_status"] == "COMPLETE"
    assert handle.observed["weights_dir"] == WEIGHTS_DIR
    assert handle.observed["fetch_s"] == 10.0  # one poll interval: RUNNING, then COMPLETE
    assert handle.state is EndpointState.READY


async def test_without_a_beam_secret_only_the_fetch_pod_gets_the_hf_token(
    provider: BeamPodProvider, fake_launcher: FakeLauncher, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec(access="gated", token_ref=HF_TOKEN_REF, **VOLUME))
    fetch, deploy = (call["request"] for call in fake_launcher.calls)
    assert fetch["env"]["HF_TOKEN"] == HF_SECRET
    assert fetch["env"]["SPLEEF_TOKEN_ENV"] == "HF_TOKEN"
    assert fetch["secrets"] == []
    assert deploy["env"] == {}
    assert handle.observed["fetch_request"]["env"]["HF_TOKEN"] == "***"
    assert HF_SECRET not in repr(handle.observed)


async def test_a_failed_weights_download_never_starts_the_gpu_pod(
    provider: BeamPodProvider, gateway: FakeBeamGateway, sdk: FakeBeamSdk, make_spec: SpecFactory
) -> None:
    gateway.task_statuses = ["RUNNING", "ERROR"]
    with pytest.raises(LaunchFailed) as caught:
        await deployed(provider, make_spec(**VOLUME))
    assert caught.value.detail["reason"] == "fetch_failed"
    assert caught.value.detail["task_status"] == "ERROR"
    assert len(sdk.pods) == 1
    assert gateway.deployments == {}


async def test_a_weights_download_past_its_deadline_is_stopped(
    provider: BeamPodProvider, gateway: FakeBeamGateway, sdk: FakeBeamSdk, make_spec: SpecFactory
) -> None:
    gateway.task_statuses = ["RUNNING"]
    with pytest.raises(LaunchFailed) as caught:
        await deployed(provider, make_spec(fetch_timeout_s=30, **VOLUME))
    assert caught.value.detail["reason"] == "fetch_timeout"
    assert gateway.stopped_containers == [caught.value.detail["fetch_container_id"]]
    assert len(sdk.pods) == 1
    assert gateway.deployments == {}


async def test_validate_refuses_a_repo_that_would_leave_the_volume(
    provider: BeamPodProvider, make_spec: SpecFactory
) -> None:
    from dataclasses import replace

    spec = make_spec(**VOLUME)
    escaping = replace(spec, source=replace(spec.source, repo="../outside"))
    report = await provider.validate(escaping)
    assert ("weights_path", False) in {(check_id, ok) for check_id, ok, _ in report.checks}


# -- multi-GPU layouts ---------------------------------------------------------------------------


async def test_eight_gpus_default_to_one_tensor_parallel_group(
    provider: BeamPodProvider, sdk: FakeBeamSdk, make_spec: SpecFactory
) -> None:
    await deployed(provider, make_spec(gpu_count=8))
    pod = sdk.pods[0]
    assert pod["gpu_count"] == 8
    entrypoint = pod["entrypoint"]
    assert entrypoint[entrypoint.index("--tp-size") + 1] == "8"


async def test_a_preset_layout_is_passed_through_without_a_second_tp_flag(
    provider: BeamPodProvider, sdk: FakeBeamSdk, make_spec: SpecFactory
) -> None:
    await deployed(
        provider, make_spec(gpu_count=4, engine_extra_args=["--tp-size", "2", "--dp-size", "2"])
    )
    entrypoint = sdk.pods[0]["entrypoint"]
    assert entrypoint.count("--tp-size") == 1
    assert entrypoint[entrypoint.index("--dp-size") + 1] == "2"


async def test_a_layout_that_does_not_fit_is_refused_before_any_launch(
    provider: BeamPodProvider, fake_launcher: FakeLauncher, make_spec: SpecFactory
) -> None:
    spec = make_spec(gpu_count=4, engine_extra_args=["--tp-size", "2"])
    report = await provider.validate(spec)
    assert ("parallelism", False) in {(check_id, ok) for check_id, ok, _ in report.checks}
    with pytest.raises(ParallelismMismatch):
        await provider.plan(spec)
    assert fake_launcher.calls == []


# -- crash loops ---------------------------------------------------------------------------------


async def test_a_container_that_keeps_restarting_without_serving_is_failed(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    gateway.container_status = "PENDING"
    states = []
    for generation in range(1, 5):
        gateway.generation = generation
        states.append(await provider.status(handle))
    assert states[:2] == [EndpointState.DEPLOYING, EndpointState.DEPLOYING]
    assert states[-1] is EndpointState.FAILED
    assert "crash loop" in handle.observed["failure"]


async def test_serving_resets_the_restart_count(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    handle = await deployed(provider, make_spec())
    for generation in range(1, 6):
        gateway.generation = generation
        gateway.container_status = "PENDING"
        assert await provider.status(handle) is EndpointState.DEPLOYING
        gateway.container_status = "RUNNING"
        assert await provider.status(handle) is EndpointState.READY


async def test_a_failed_launch_reports_what_the_sdk_printed(
    gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    async def refused(
        argv: Sequence[str], stdin: bytes, env: Mapping[str, str], cwd: str, timeout_s: float
    ) -> tuple[int, bytes, bytes]:
        result = (
            RESULT_PREFIX
            + '{"ok": false, "error": "the Beam SDK reported that the deployment failed"}'
        )
        said = f"=> Preparing image\n{result}\n"
        return 1, said.encode(), f"✗ Multi-GPU is not enabled for token {TOKEN}\n".encode()

    provider = BeamPodProvider(
        "acme",
        {"api_key_ref": TOKEN_REF, "image": IMAGE},
        DictSecretResolver({TOKEN_REF: TOKEN}),
        launch_runner=refused,
        api_transport=gateway.transport(),
    )
    with pytest.raises(LaunchFailed) as caught:
        await deployed(provider, make_spec(gpu_count=2))
    assert "Multi-GPU is not enabled" in str(caught.value)
    assert TOKEN not in str(caught.value) and TOKEN not in caught.value.detail["sdk_output_tail"]
    assert "Preparing image" in caught.value.detail["sdk_output_tail"]


async def test_validate_refuses_multi_gpu_when_the_workspace_does_not_allow_it(
    provider: BeamPodProvider, gateway: FakeBeamGateway, make_spec: SpecFactory
) -> None:
    def multi_gpu(report_checks: tuple[tuple[str, bool, str], ...]) -> bool | None:
        return next((ok for check_id, ok, _ in report_checks if check_id == "multi_gpu"), None)

    assert multi_gpu((await provider.validate(make_spec())).checks) is None
    assert multi_gpu((await provider.validate(make_spec(gpu_count=2))).checks) is True
    gateway.max_gpu_count = 2
    assert multi_gpu((await provider.validate(make_spec(gpu_count=4))).checks) is False
    gateway.multi_gpu_enabled = False
    report = await provider.validate(make_spec(gpu_count=2))
    assert multi_gpu(report.checks) is False and not report.ok
