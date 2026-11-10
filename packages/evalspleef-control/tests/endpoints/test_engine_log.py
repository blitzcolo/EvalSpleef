"""Engine logs are kept at teardown, without credentials, and never block the teardown."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from evalspleef.control.endpoints import EngineLogStore, make_endpoint_id
from evalspleef.spi.model import EndpointState, ModelSpec
from evalspleef.testing.secrets import DictSecretResolver

MakeWorld = Callable[..., Awaitable[Any]]
SpecFactory = Callable[..., ModelSpec]

HF_TOKEN_REF = "vault://hf/eval"
ENDPOINT_KEY_REF = "vault://models/qwen"  # the mock provider's default credential reference
HF_SECRET = "hf_planted_token_0123456789"
KEY_SECRET = "sk-planted-endpoint-key-9876543210"


def _store(root: Path, **kwargs: int) -> EngineLogStore:
    secrets = DictSecretResolver({HF_TOKEN_REF: HF_SECRET, ENDPOINT_KEY_REF: KEY_SECRET})
    return EngineLogStore(root / "endpoint-logs", secrets, **kwargs)


def _read(path: str) -> bytes:
    return Path(path).read_bytes()


async def _retire(
    world: Any, spec: ModelSpec, store: EngineLogStore
) -> tuple[EndpointState, dict[str, Any]]:
    handle = await world.manager.register(spec)
    retired = await world.manager.retire(handle.endpoint_id, "terminate", engine_logs=store)
    return retired.state, (await world.events_in())[-1].payload["engine_log"]


async def test_the_log_is_stored_without_credentials_and_recorded_in_the_retire_event(
    make_world: MakeWorld, spec_factory: SpecFactory, tmp_path: Path
) -> None:
    text = f"engine up\nHF_TOKEN={HF_SECRET}\nAuthorization: Bearer {KEY_SECRET}\nready\n"
    world = await make_world(mode="api", provider_params={"engine_log": text})

    spec = spec_factory(hf=True)

    state, record = await _retire(world, spec, _store(tmp_path))

    assert state is EndpointState.RETIRED
    assert record["status"] == "stored"
    stored = Path(record["path"])
    assert stored.parent == tmp_path / "endpoint-logs" / make_endpoint_id(spec)
    data = _read(record["path"])
    assert HF_SECRET.encode() not in data and KEY_SECRET.encode() not in data
    assert data.startswith(b"engine up\nHF_TOKEN=***")
    assert record["redactions"] == 2
    assert stat.S_IMODE(os.stat(record["path"]).st_mode) == 0o600
    assert (record["sha256"], record["bytes"]) == (hashlib.sha256(data).hexdigest(), len(data))
    assert record["truncated"] is False


async def test_a_later_retire_without_a_store_does_not_inherit_the_earlier_log_outcome(
    make_world: MakeWorld, spec_factory: SpecFactory, tmp_path: Path
) -> None:
    world = await make_world(mode="api", provider_params={"engine_log": "engine up"})
    handle = await world.manager.register(spec_factory())
    await world.manager.probe(handle.endpoint_id)  # READY, which may sleep

    await world.manager.retire(handle.endpoint_id, "sleep", engine_logs=_store(tmp_path))
    await world.manager.retire(handle.endpoint_id, "terminate")

    first, second = (await world.events_in())[-2:]
    assert first.payload["engine_log"]["status"] == "stored"
    assert second.payload["state"] == "retired" and "engine_log" not in second.payload


async def test_two_logs_of_one_endpoint_in_the_same_second_keep_separate_files(
    make_world: MakeWorld, spec_factory: SpecFactory, tmp_path: Path
) -> None:
    world = await make_world(mode="api", provider_params={"engine_log": "engine up"})
    handle = await world.manager.register(spec_factory())
    await world.manager.probe(handle.endpoint_id)  # READY, which may sleep

    await world.manager.retire(handle.endpoint_id, "sleep", engine_logs=_store(tmp_path))
    await world.manager.retire(handle.endpoint_id, "terminate", engine_logs=_store(tmp_path))

    first, second = (await world.events_in())[-2:]
    assert first.payload["engine_log"]["path"] != second.payload["engine_log"]["path"]
    assert second.payload["engine_log"]["status"] == "stored"


async def test_a_log_over_the_bound_is_kept_as_its_newest_part_and_flagged(
    make_world: MakeWorld, spec_factory: SpecFactory, tmp_path: Path
) -> None:
    world = await make_world(mode="api", provider_params={"engine_log": "old-" * 50 + "newest"})

    _, record = await _retire(world, spec_factory(), _store(tmp_path, max_bytes=20))

    assert record["truncated"] is True and record["bytes"] <= 20
    assert _read(record["path"]).endswith(b"newest")


async def test_a_provider_without_logs_records_unsupported_and_still_retires(
    make_world: MakeWorld, spec_factory: SpecFactory, tmp_path: Path
) -> None:
    world = await make_world(mode="api")

    state, record = await _retire(world, spec_factory(), _store(tmp_path))

    assert (state, record) == (EndpointState.RETIRED, {"status": "unsupported"})
    assert not (tmp_path / "endpoint-logs").exists()


async def test_a_failed_fetch_is_recorded_without_secrets_and_does_not_block_teardown(
    make_world: MakeWorld, spec_factory: SpecFactory, tmp_path: Path
) -> None:
    world = await make_world(
        mode="api", provider_params={"engine_log_error": f"log API refused {KEY_SECRET}"}
    )

    state, record = await _retire(world, spec_factory(), _store(tmp_path))

    assert state is EndpointState.RETIRED
    assert record["status"] == "error" and "log API refused ***" in record["error"]
    assert KEY_SECRET not in record["error"]
