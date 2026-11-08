"""spleef config validate: acceptance, exit codes, cross-reference checks."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from evalspleef.control.cli.app import app

TASKS = Path(__file__).resolve().parents[4] / "tasks"


def _validate(runner: CliRunner, config: Path) -> object:
    return runner.invoke(
        app,
        [
            "config",
            "validate",
            "--models",
            str(config / "models.yaml"),
            "--providers",
            str(config / "providers.yaml"),
            "--batch",
            str(config / "batch.yaml"),
            "--tasks-dir",
            str(TASKS),
        ],
    )


def test_validate_accepts_example_configs(cli_runner: CliRunner, env: Path) -> None:
    result = _validate(cli_runner, env)
    assert result.exit_code == 0, result.output
    assert "config ok" in result.output


def test_validate_rejects_unknown_batch_model(
    cli_runner: CliRunner, env: Path, batch_yaml: str
) -> None:
    (env / "batch.yaml").write_text(
        batch_yaml.replace("models: [demo]", "models: [demo, ghost]"), encoding="utf-8"
    )
    result = _validate(cli_runner, env)
    assert result.exit_code == 2
    assert "unknown model 'ghost'" in result.output


def test_validate_rejects_unknown_account(
    cli_runner: CliRunner, env: Path, models_yaml: str
) -> None:
    (env / "models.yaml").write_text(
        models_yaml.replace("account: fake", "account: nowhere"), encoding="utf-8"
    )
    result = _validate(cli_runner, env)
    assert result.exit_code == 2
    assert "unknown model account" in result.output


def test_validate_rejects_unknown_shell(cli_runner: CliRunner, env: Path, batch_yaml: str) -> None:
    (env / "batch.yaml").write_text(
        batch_yaml.replace("unified: scripted", "unified: no-such-shell"), encoding="utf-8"
    )
    result = _validate(cli_runner, env)
    assert result.exit_code == 2
    assert "no agent runner installed for shell 'no-such-shell'" in result.output


def test_validate_malformed_config_is_usage_error(cli_runner: CliRunner, env: Path) -> None:
    (env / "batch.yaml").write_text("batch: not-a-batch-id\n", encoding="utf-8")
    result = _validate(cli_runner, env)
    assert result.exit_code == 2
    assert "configuration error" in result.output


def test_validate_unknown_task_is_usage_error(
    cli_runner: CliRunner, env: Path, batch_yaml: str
) -> None:
    (env / "batch.yaml").write_text(
        batch_yaml.replace("include: [logic-001]", "include: [ghost-001]"), encoding="utf-8"
    )
    result = _validate(cli_runner, env)
    assert result.exit_code == 2


def test_models_yaml_placeholders_may_be_unset(
    cli_runner: CliRunner, env: Path, providers_yaml: str
) -> None:
    # config validate accepts environment placeholders that are not exported yet.
    (env / "providers.yaml").write_text(
        providers_yaml.format(
            base_url="${SPLEEF_FAKE_BASE_URL}", gateway_url="${SPLEEF_GATEWAY_URL}"
        ),
        encoding="utf-8",
    )
    result = _validate(cli_runner, env)
    assert result.exit_code == 0, result.output
    assert "config ok" in result.output


def test_config_validate_json(cli_runner: CliRunner, env: Path) -> None:
    result = cli_runner.invoke(
        app,
        [
            "config",
            "validate",
            "--json",
            "--models",
            str(env / "models.yaml"),
            "--providers",
            str(env / "providers.yaml"),
            "--batch",
            str(env / "batch.yaml"),
            "--tasks-dir",
            str(TASKS),
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"ok": true' in result.output
