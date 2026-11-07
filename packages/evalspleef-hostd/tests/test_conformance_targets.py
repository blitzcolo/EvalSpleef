from __future__ import annotations

import warnings

from evalspleef.hostd import conformance


async def test_remote_mock_target_is_built_and_docker_absence_is_reported() -> None:
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        targets = conformance.build_targets()
    try:
        names = [target.name for target in targets]
        assert names[0] == "remote-mock"
        if "remote-docker" in names:
            assert "remote-docker" not in conformance.UNAVAILABLE
        else:
            assert conformance.UNAVAILABLE["remote-docker"]
            assert any("not built" in str(w.message) for w in recorded)
        mock_target = targets[0]
        async with mock_target.open_sandbox("conf-target") as sandbox:
            assert (await sandbox.exec(["echo", "x"])).stdout.strip() == b"x"
    finally:
        for target in targets:
            assert target.cleanup is not None
            await target.cleanup()
