# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Builds a Lambda layer zip from an arbitrary worktree.

lambda-layer/src/build-lambda-layer.sh hardcodes ../../aws-opentelemetry-distro, which is the checkout
it lives in. Benchmarking two refs needs a layer per ref, so this assembles the same build context with
the distro copied from a given worktree instead. The Dockerfile and wrapper scripts are taken from the
worktree too, so a ref that changes them is measured as it would ship.

Nothing here touches AWS. The output is a zip on local disk.
"""
import shutil
import subprocess
from pathlib import Path
from typing import Optional

_LAYER_SRC = "lambda-layer/src"
_DISTRO = "aws-opentelemetry-distro"
_ZIP_NAME = "aws-opentelemetry-python-layer.zip"
# Each stage pulls a SAM base image from public.ecr.aws, which rate-limits with HTTP 429 under load --
# the same limit that turned this PR's CI red. Builds are cached per ref to pull as rarely as possible.
_BUILD_TIMEOUT_S = 2700


class LayerBuildError(RuntimeError):
    pass


def build(worktree: Path, out_dir: Path, tag: str, reuse: bool = True) -> Path:
    """Build the layer zip for one worktree and return its path.

    reuse=True returns an existing zip untouched, so re-running a plan does not rebuild a ref whose
    layer is already on disk.
    """
    worktree = Path(worktree).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{tag}-{_ZIP_NAME}"
    if reuse and zip_path.exists():
        return zip_path

    layer_src = worktree / _LAYER_SRC
    distro_src = worktree / _DISTRO
    for required in (layer_src / "Dockerfile", distro_src / "pyproject.toml"):
        if not required.exists():
            raise LayerBuildError(f"{required} missing; is {worktree} a full checkout?")

    context = out_dir / f"context-{tag}"
    if context.exists():
        shutil.rmtree(context)
    # Mirror build-lambda-layer.sh: the Dockerfile expects the distro inside its own build context
    shutil.copytree(layer_src, context, ignore=shutil.ignore_patterns("build", "tests", "__pycache__"))
    shutil.copytree(distro_src, context / _DISTRO, ignore=shutil.ignore_patterns("build", "dist", "__pycache__"))

    image = f"aws-opentelemetry-python-layer:{tag}"
    staging = out_dir / f"staging-{tag}"
    try:
        _run(["docker", "build", "--progress", "plain", "-t", image, "."], cwd=context)
        staging.mkdir(parents=True, exist_ok=True)
        _run(["docker", "run", "--rm", "-v", f"{staging}:/out", image], cwd=context)

        produced = staging / _ZIP_NAME
        if not produced.exists():
            raise LayerBuildError(f"build finished but {produced} was not produced")
        shutil.move(str(produced), zip_path)
    finally:
        # Leave no half-built context behind for the next run to trip over
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(context, ignore_errors=True)
    return zip_path


def _run(command: list, cwd: Optional[Path] = None) -> None:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=_BUILD_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0:
        tail = "\n".join((result.stderr or result.stdout or "").strip().splitlines()[-25:])
        hint = ""
        if any(marker in tail.lower() for marker in ("429", "toomanyrequests", "403 forbidden", "denied")):
            hint = (
                " -- public.ecr.aws refused the pull. Authenticate first:\n"
                "    aws ecr-public get-login-password --region us-east-1 "
                "| docker login --username AWS --password-stdin public.ecr.aws"
            )
        raise LayerBuildError(f"{' '.join(command)} failed with {result.returncode}{hint}:\n{tail}")
