#!/usr/bin/env python3
"""EvalSpleef environment template builder.

Builds Linux container templates via docker CLI, collects container
fingerprints (packages hash, user, permissions, PATH), and updates
templates/registry.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_templates")

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
REGISTRY_PATH = TEMPLATES_DIR / "registry.json"

FINGERPRINT_SCRIPT = r"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

def run_cmd(args):
    try:
        res = subprocess.run(args, capture_output=True, text=True, check=False)
        return res.stdout.strip()
    except Exception:
        return ""

# 1. Packages hash
dpkg_out = run_cmd(["dpkg-query", "-W"])
pip_out = run_cmd(["python3", "-m", "pip", "list"])
combined_pkgs = f"{dpkg_out}\n---\n{pip_out}"
pkg_hash = hashlib.sha256(combined_pkgs.encode("utf-8")).hexdigest()

# 2. Toolchain versions
python_ver = run_cmd(["python3", "--version"])
bash_ver = run_cmd(["bash", "--version"]).splitlines()[0] if run_cmd(["bash", "--version"]) else ""
patch_ver = run_cmd(["patch", "--version"]).splitlines()[0] if run_cmd(["patch", "--version"]) else ""

# 3. User & identity
uid = os.getuid()
gid = os.getgid()
id_str = run_cmd(["id"])

# 4. Permissions
workspace_writable = os.access("/workspace", os.W_OK)
home_dir = os.environ.get("HOME", "")
home_writable = os.access(home_dir, os.W_OK) if home_dir else False
sudo_present = shutil.which("sudo") is not None

# Count any residual setuid/setgid binaries on rootfs
setuid_res = run_cmd(["find", "/", "-xdev", "-perm", "/6000", "-type", "f"])
setuid_count = len(setuid_res.splitlines()) if setuid_res else 0

# 5. Environment PATH
path_val = os.environ.get("PATH", "")

fingerprint = {
    "packages_hash": pkg_hash,
    "toolchains": {
        "python": python_ver,
        "bash": bash_ver,
        "patch": patch_ver,
    },
    "user": {
        "uid": uid,
        "gid": gid,
        "id": id_str,
    },
    "permissions": {
        "workspace_writable": workspace_writable,
        "home_writable": home_writable,
        "sudo_present": sudo_present,
        "setuid_count": setuid_count,
    },
    "path": path_val,
}

print(json.dumps(fingerprint))
"""


def load_registry(path: Path) -> dict[str, Any]:
    """Load registry file if it exists, else return empty dict."""
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning("Failed to load existing registry from %s: %s", path, e)
    return {}


def save_registry(path: Path, registry: dict[str, Any]) -> None:
    """Save registry atomically to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, sort_keys=True)
        f.write("\n")
    temp_path.replace(path)
    logger.info("Saved registry to %s (%d entries)", path, len(registry))


def parse_env_yaml(env_dir: Path) -> dict[str, Any]:
    """Parse env.yaml if present."""
    yaml_file = env_dir / "env.yaml"
    if not yaml_file.exists():
        return {}
    # Use simple yaml parsing or fallback
    try:
        import yaml  # type: ignore

        with open(yaml_file, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        # Fallback rudimentary line parser if PyYAML is absent
        res: dict[str, Any] = {}
        with open(yaml_file, encoding="utf-8") as f:
            for line in f:
                if ":" in line and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    res[k.strip()] = v.strip().strip("'\"")
        return res


def build_image(env_id: Path, tag: str) -> None:
    """Execute docker build command."""
    cmd = ["docker", "build", "-t", tag, str(env_id)]
    logger.info("Building image for %s: %s", env_id.name, " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        logger.error("docker build failed for %s:\n%s\n%s", env_id.name, res.stdout, res.stderr)
        raise RuntimeError(f"docker build failed for {env_id.name} with exit code {res.returncode}")
    logger.info("Successfully built %s", tag)


def inspect_image(tag: str) -> tuple[str, int]:
    """Inspect local image and return (image_id, size_bytes)."""
    cmd = ["docker", "inspect", tag, "--format", "{{.Id}} {{.Size}}"]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    parts = res.stdout.strip().split()
    if len(parts) < 2:
        raise ValueError(f"Unexpected inspect output: {res.stdout.strip()}")
    image_id = parts[0]
    size_bytes = int(parts[1])
    return image_id, size_bytes


def run_env_fingerprint(tag: str) -> dict[str, Any]:
    """Run fingerprint script inside the newly built container."""
    cmd = [
        "docker",
        "run",
        "--rm",
        "--label",
        "spleef",
        tag,
        "python3",
        "-c",
        FINGERPRINT_SCRIPT,
    ]
    logger.info("Running env-fingerprint inside %s", tag)
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        logger.error("Fingerprint probe failed on %s:\n%s\n%s", tag, res.stdout, res.stderr)
        raise RuntimeError(f"Fingerprint failed for {tag} with exit code {res.returncode}")
    try:
        data = json.loads(res.stdout.strip())
        return data
    except json.JSONDecodeError as err:
        logger.error("Invalid JSON from fingerprint: %s", res.stdout)
        raise RuntimeError(f"Invalid JSON from fingerprint: {res.stdout}") from err


def build_and_register_env(
    env_id: str,
    templates_dir: Path,
    registry: dict[str, Any],
    built_at: str,
) -> dict[str, Any]:
    """Build single environment template and update registry record."""
    env_dir = templates_dir / env_id
    dockerfile = env_dir / "Dockerfile"
    if not dockerfile.exists():
        raise FileNotFoundError(f"Dockerfile not found at {dockerfile}")

    meta = parse_env_yaml(env_dir)
    tag = meta.get("tag") or f"llm-eval/{env_id}:local"

    build_image(env_dir, tag)
    image_id, size_bytes = inspect_image(tag)
    fingerprint = run_env_fingerprint(tag)

    record: dict[str, Any] = {
        "tag": tag,
        "image_id": image_id,
        "size_bytes": size_bytes,
        "built_at": built_at,
        "fingerprint": fingerprint,
    }
    registry[env_id] = record
    logger.info(
        "Registered %s -> image_id=%s, size=%d bytes, uid=%s",
        env_id,
        image_id[:19],
        size_bytes,
        fingerprint.get("user", {}).get("uid"),
    )
    return record


def find_all_linux_envs(templates_dir: Path) -> list[str]:
    """Find all subdirectories of templates_dir containing a Dockerfile."""
    envs: list[str] = []
    for d in sorted(templates_dir.iterdir()):
        if d.is_dir() and (d / "Dockerfile").exists():
            envs.append(d.name)
    return envs


def main() -> int:
    parser = argparse.ArgumentParser(description="Build EvalSpleef Linux environment templates.")
    parser.add_argument(
        "--env",
        action="append",
        dest="envs",
        help="Environment ID to build (can be specified multiple times).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Build all Linux container templates found under templates/.",
    )
    parser.add_argument(
        "--built-at",
        default=None,
        help="Injected clock timestamp (ISO 8601). Defaults to current UTC time.",
    )
    parser.add_argument(
        "--registry",
        default=str(REGISTRY_PATH),
        help="Path to registry.json file.",
    )

    args = parser.parse_args()

    target_envs: list[str] = []
    if args.all:
        target_envs = find_all_linux_envs(TEMPLATES_DIR)
    elif args.envs:
        target_envs = args.envs
    else:
        parser.error("Must specify at least one --env ID or --all")

    built_at = args.built_at or datetime.now(UTC).isoformat()
    reg_path = Path(args.registry)
    registry = load_registry(reg_path)

    for env_id in target_envs:
        build_and_register_env(env_id, TEMPLATES_DIR, registry, built_at)

    save_registry(reg_path, registry)
    logger.info("Build completed successfully for %s", target_envs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
