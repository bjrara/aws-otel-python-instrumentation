# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The throwaway half: code that knows what this particular change does.

An instrument is generated per run and loaded from a file path inside the run's sandbox, not imported
from the repo. Nothing change-specific is persisted for reuse -- a datagram counter written for one PR
has no business accumulating in a benchmark harness. What persists instead is the environment that
runs it, the capabilities it must assert, and the conformance gate that validates it.

A generated module declares:

    REQUIRES = {"udp_datagram_bytes": 65507}   # capability keys, checked before anything runs
    SUPPORTS = ("local_process",)              # environment names it knows how to drive

    def workload(scenario) -> dict             # the input: span shape, invoke event, request mix
    def drive(scenario, services) -> dict      # in-process environments only; returns raw counters
    def observe(raw, scenario) -> dict         # raw -> the change-specific metrics a plan names

Because it is generated, it is assumed wrong until a conformance run says otherwise.
"""
import importlib.util
from pathlib import Path
from typing import Dict, Protocol


class InstrumentError(RuntimeError):
    pass


class Instrument(Protocol):
    REQUIRES: Dict[str, object]
    SUPPORTS: tuple

    def workload(self, scenario) -> Dict:
        """The input this scenario should present to the environment."""

    def observe(self, raw: Dict, scenario) -> Dict[str, float]:
        """Turn the environment's raw output into change-specific metrics."""


def load(path: Path):
    """Import a generated instrument from a file path, outside the package tree."""
    path = Path(path).resolve()
    if not path.exists():
        raise InstrumentError(f"instrument not found at {path}")

    spec = importlib.util.spec_from_file_location(f"benchmark_instrument_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise InstrumentError(f"{path} is not importable as a module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for attribute in ("REQUIRES", "SUPPORTS", "workload", "observe"):
        if not hasattr(module, attribute):
            raise InstrumentError(f"{path} does not define {attribute}")
    if not isinstance(module.REQUIRES, dict):
        raise InstrumentError(f"{path}: REQUIRES must be a dict of capability keys")
    if not module.SUPPORTS:
        raise InstrumentError(f"{path}: SUPPORTS must name at least one environment")
    return module


def supports(module, environment_name: str) -> None:
    if environment_name not in tuple(module.SUPPORTS):
        raise InstrumentError(
            f"instrument supports {tuple(module.SUPPORTS)}, not {environment_name!r}; "
            "generate one for this environment or pick another environment"
        )
