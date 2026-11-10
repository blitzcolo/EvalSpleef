"""Tests for the control-plane configuration loaders."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from evalspleef.control.config import (
    BatchFileConfig,
    ConfigError,
    HFSourceConfig,
    ModelConfig,
    ProvidersFileConfig,
    load_batch,
    load_hf_token_expiry,
    load_models,
    load_providers,
)
from evalspleef.spi.model import HFSource, VendorSource

SHA = "3f9c2b7d1e" * 4  # 40 hex chars

MODELS_YAML = f"""
models:
  - slug: qwen3-32b-ft-sql
    family: qwen
    roles: [target]
    source:
      type: huggingface
      repo: our-org/qwen3-32b-sql-sft
      revision: {SHA}
      access: private
      token_ref: vault://secret/hf/eval-preflight
      mirror: true
    deployment:
      adapter: novita-dedicated
      account: novita-main
      params:
        gpu_type: H100-SXM-80GB
        gpu_count: 1
    declared_context_tokens: 131072
    declared_max_output_tokens: 16384
    pricing_ref: ref:novita-serverless/qwen3-32b@2026-11
    license: apache-2.0
    owner: model-ops

  - slug: claude-sonnet-5
    family: claude
    roles: [judge, target]
    source: {{type: vendor, vendor: anthropic, model_id: claude-sonnet-5-20261101}}
    deployment:
      adapter: openai-compatible
      account: anthropic-official
      params: {{protocol: anthropic_messages}}
    declared_context_tokens: 200000
    declared_max_output_tokens: 64000
    pricing_ref: api:anthropic@2026-11
    license: proprietary
    owner: eval-lead
"""

PROVIDERS_YAML = """
model_accounts:
  novita-main: {adapter: novita-dedicated, api_key_ref: vault://secret/novita/main}
  anthropic-official:
    adapter: openai-compatible
    base_url: https://api.anthropic.com
    api_key_ref: vault://secret/anthropic/eval

sandbox_instances:
  novita-us-phx-1:
    adapter: novita-sandbox
    config: {region: us-phx-1, api_key_ref: vault://secret/novita/main}
    limits: {max_concurrency: 80}
  macmini-01:
    adapter: remote
    config: {host: macmini-01, providers: [tart, docker]}
    limits: {tart: {max_concurrency: 2}}
  local-docker:
    adapter: docker
    config: {runtime: runc}

routes:
  - when: {os: linux}
    prefer: [novita-us-phx-1, local-docker, macmini-01/docker]
  - when: {os: macos}
    prefer: [macmini-01/tart]
  - when: {role: verifier}
    prefer: [local-docker, novita-us-phx-1]

egress_proxies:
  cloud: {url: http://egress.internal:3128, public_ip: 203.0.113.10}
  macmini-01: {url: http://192.168.64.1:3128}

gateway:
  internal_url: http://gateway.internal:4000
  public_url: https://gw.example.com
"""

BATCH_YAML = """
batch: 2026-12-b1
models: [qwen3-32b-ft-sql, claude-sonnet-5]
shells: {unified: terminus, native: {claude-sonnet-5: claude-code}}
tasks: {include: [logic-*, web-*]}
epochs: 3
budget: {max_total_usd: 8000}
endpoint_policy: {warm_before_run: true, retire_after: sleep}
judges: [claude-sonnet-5, gpt-5-codex]
pricing_table: 2026-11
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_load_models_converts_to_spi(tmp_path: Path) -> None:
    path = _write(tmp_path, "models.yaml", MODELS_YAML)
    specs = load_models(path)
    assert [s.slug for s in specs] == ["qwen3-32b-ft-sql", "claude-sonnet-5"]

    hf = specs[0]
    assert isinstance(hf.source, HFSource)
    assert hf.source.revision == SHA
    assert hf.source.access == "private"
    assert hf.source.token_ref == "vault://secret/hf/eval-preflight"
    assert hf.roles == frozenset({"target"})
    assert hf.spec_version == 1
    assert hf.deployment.adapter == "novita-dedicated"
    assert hf.deployment.params["gpu_count"] == 1

    vendor = specs[1]
    assert isinstance(vendor.source, VendorSource)
    assert vendor.source.vendor == "anthropic"
    assert vendor.roles == frozenset({"judge", "target"})


def _hf_entry(slug: str, token_lines: str) -> str:
    return f"""
  - slug: {slug}
    family: qwen
    roles: [target]
    source:
      type: huggingface
      repo: our-org/{slug}
      revision: {SHA}
      access: gated
{token_lines}
    deployment: {{adapter: novita-dedicated, account: novita-main}}
    declared_context_tokens: 32768
    declared_max_output_tokens: 4096
    pricing_ref: ref:novita/{slug}@2026-11
    license: apache-2.0
    owner: model-ops
"""


def test_hf_token_expiry_is_read_from_models_yaml(tmp_path: Path) -> None:
    shared = "      token_ref: vault://hf/shared\n"
    text = "models:" + "".join(
        [
            _hf_entry("alpha", shared + "      token_expires: 2027-03-01"),
            _hf_entry("beta", shared + "      token_expires: never"),
            _hf_entry("gamma", "      token_ref: vault://hf/forever\n      token_expires: never"),
            _hf_entry("delta", "      token_ref: vault://hf/unknown"),
        ]
    )
    path = tmp_path / "models.yaml"
    path.write_text(text, encoding="utf-8")

    # Two models sharing a token: the earlier date is the one the batch depends on.
    assert load_hf_token_expiry(path) == {
        "vault://hf/shared": date(2027, 3, 1),
        "vault://hf/forever": date.max,
    }


def test_token_expires_needs_the_token_it_describes(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("models:" + _hf_entry("alpha", "      token_expires: 2027-03-01"), "utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_models(path)
    assert "token_ref" in str(excinfo.value)


def test_revision_must_be_40_hex(tmp_path: Path) -> None:
    bad = MODELS_YAML.replace(SHA, "main")
    path = _write(tmp_path, "models.yaml", bad)
    with pytest.raises(ConfigError) as excinfo:
        load_models(path)
    err = excinfo.value
    assert err.file == str(path)
    assert "models[0].source" in err.path
    assert "40-hex" in str(err)


def test_revision_branch_allowed_with_flag(tmp_path: Path) -> None:
    flagged = MODELS_YAML.replace(
        f"revision: {SHA}", "revision: main\n      unresolved_branch: true"
    )
    path = _write(tmp_path, "models.yaml", flagged)
    specs = load_models(path)
    assert specs[0].source.revision == "main"


def test_uppercase_sha_rejected(tmp_path: Path) -> None:
    bad = MODELS_YAML.replace(SHA, SHA.upper())
    path = _write(tmp_path, "models.yaml", bad)
    with pytest.raises(ConfigError):
        load_models(path)


def test_error_names_file_and_path_for_scalars(tmp_path: Path) -> None:
    bad = MODELS_YAML.replace("declared_context_tokens: 131072", "declared_context_tokens: -5")
    path = _write(tmp_path, "models.yaml", bad)
    with pytest.raises(ConfigError) as excinfo:
        load_models(path)
    assert excinfo.value.file == str(path)
    assert excinfo.value.path == "models[0].declared_context_tokens"


def test_invalid_yaml_reports_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "models.yaml", "models: [unclosed\n")
    with pytest.raises(ConfigError) as excinfo:
        load_models(path)
    assert excinfo.value.file == str(path)
    assert "invalid YAML" in str(excinfo.value)


def test_missing_file_reports_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_models(tmp_path / "nope.yaml")
    assert "cannot read file" in str(excinfo.value)


def test_bad_secret_ref_scheme(tmp_path: Path) -> None:
    bad = MODELS_YAML.replace("vault://secret/hf/eval-preflight", "plaintext-secret")
    path = _write(tmp_path, "models.yaml", bad)
    with pytest.raises(ConfigError) as excinfo:
        load_models(path)
    assert "token_ref" in excinfo.value.path


def test_load_providers(tmp_path: Path) -> None:
    path = _write(tmp_path, "providers.yaml", PROVIDERS_YAML)
    config = load_providers(path)
    assert isinstance(config, ProvidersFileConfig)
    assert config.model_accounts["novita-main"].adapter == "novita-dedicated"
    assert config.sandbox_instances["macmini-01"].config["providers"] == ["tart", "docker"]
    assert [str(r.when.os) for r in config.routes[:2]] == ["linux", "macos"]
    assert config.routes[0].prefer == ["novita-us-phx-1", "local-docker", "macmini-01/docker"]
    assert config.egress_proxies["cloud"].public_ip == "203.0.113.10"
    assert config.gateway is not None
    assert config.gateway.internal_url == "http://gateway.internal:4000"


def test_route_when_requires_a_condition(tmp_path: Path) -> None:
    bad = PROVIDERS_YAML.replace("  - when: {os: macos}\n", "  - when: {}\n")
    path = _write(tmp_path, "providers.yaml", bad)
    with pytest.raises(ConfigError) as excinfo:
        load_providers(path)
    assert "when" in excinfo.value.path


def test_load_batch(tmp_path: Path) -> None:
    path = _write(tmp_path, "batch.yaml", BATCH_YAML)
    batch = load_batch(path)
    assert isinstance(batch, BatchFileConfig)
    assert batch.batch == "2026-12-b1"
    assert batch.epochs == 3
    assert batch.shells.unified == "terminus"
    assert batch.shells.native == {"claude-sonnet-5": "claude-code"}
    assert batch.tasks.include == ["logic-*", "web-*"]
    assert batch.endpoint_policy.warm_before_run is True
    assert batch.judges == ["claude-sonnet-5", "gpt-5-codex"]


def test_agent_judge_section_defaults_to_the_batch_judges(tmp_path: Path) -> None:
    assert load_batch(_write(tmp_path, "a.yaml", BATCH_YAML)).agent_judge_slugs == []
    configured = BATCH_YAML + "agent_judge: {k: 2, max_steps: 8, max_invalid_replies: 2}\n"
    batch = load_batch(_write(tmp_path, "b.yaml", configured))
    assert batch.agent_judge is not None
    assert (batch.agent_judge.k, batch.agent_judge.max_steps) == (2, 8)
    assert batch.agent_judge.max_invalid_replies == 2
    assert batch.agent_judge.wall_clock_s == 300.0
    assert batch.agent_judge_slugs == ["claude-sonnet-5", "gpt-5-codex"]


@pytest.mark.parametrize(
    "section",
    [
        "agent_judge: {k: 0}",
        "agent_judge: {max_turns: 3}",
        "agent_judge: {output_cap_bytes: 10}",
        "agent_judge: {max_invalid_replies: -1}",
    ],
)
def test_agent_judge_section_is_validated(tmp_path: Path, section: str) -> None:
    with pytest.raises(ConfigError):
        load_batch(_write(tmp_path, "batch.yaml", BATCH_YAML + section + "\n"))


def test_agent_judge_needs_some_judge(tmp_path: Path) -> None:
    text = BATCH_YAML.replace("judges: [claude-sonnet-5, gpt-5-codex]\n", "") + "agent_judge: {}\n"
    with pytest.raises(ConfigError, match="agent_judge needs judges"):
        load_batch(_write(tmp_path, "batch.yaml", text))


def test_batch_id_format(tmp_path: Path) -> None:
    bad = BATCH_YAML.replace("2026-12-b1", "batch-2026-12")
    path = _write(tmp_path, "batch.yaml", bad)
    with pytest.raises(ConfigError) as excinfo:
        load_batch(path)
    assert excinfo.value.path == "batch"


def test_hf_source_config_direct() -> None:
    source = HFSourceConfig(
        repo="org/name",
        revision=SHA,
        access="gated",
        token_ref="env://HF_TOKEN",
    )
    assert source.unresolved_branch is False


def test_model_config_minimal_vendor() -> None:
    entry = ModelConfig(
        slug="kimi-k2.5".replace(".", "-"),
        family="kimi",
        roles=["judge"],
        source={"type": "vendor", "vendor": "moonshot", "model_id": "kimi-k2.5"},
        deployment={"adapter": "openai-compatible", "account": "main"},
        declared_context_tokens=64000,
        declared_max_output_tokens=8192,
        pricing_ref="api:moonshot@2026-11",
        license="proprietary",
        owner="eval-lead",
    )
    spec = entry.to_model_spec()
    assert spec.roles == frozenset({"judge"})
