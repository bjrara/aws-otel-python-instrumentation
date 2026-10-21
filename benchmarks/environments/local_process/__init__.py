# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""In-process environment: runs an instrument against the ref's code on this host.

Provisioned by being a Linux host -- nothing to deploy and nothing to tear down. What it offers an
instrument is a telemetry sink, resource sampling, and an honest account of what this host can
represent, which is the part that matters: a host that cannot carry a production-sized datagram will
otherwise return a confident wrong number.

Change-agnostic on purpose. Everything that knows what a particular PR does lives in the generated
instrument, not here.
"""
from dataclasses import dataclass
from typing import Dict

from benchmarks.runner import capabilities
from benchmarks.runner.environment import RunContext
from benchmarks.runner.resources import ResourceSampler

from . import host
from .udp_sink import DEFAULT_RCVBUF, UdpSink

name = "local_process"


@dataclass
class Services:
    """What an instrument may use. Anything not here, it should not reach for."""

    udp_sink = staticmethod(UdpSink)
    resource_sampler = staticmethod(ResourceSampler)
    default_rcvbuf: int = DEFAULT_RCVBUF

    @property
    def capabilities(self) -> Dict[str, object]:
        return capabilities_offered()


def capabilities_offered() -> Dict[str, object]:
    return {
        capabilities.UDP_DATAGRAM_BYTES: host.max_sendable_datagram(),
        capabilities.PROCESS_RESOURCE_SAMPLING: True,
        capabilities.CONFIGURABLE_RECEIVE_BUFFER: True,
        # A loopback sink is not the X-Ray daemon; absolute delivery rates measured here are a
        # property of this harness, not a prediction about production.
        capabilities.REAL_TELEMETRY_BACKEND: False,
    }


def execute(instrument, scenario, context: RunContext) -> Dict:
    """Let the instrument drive the code under test, after checking the host can represent it."""
    offered = capabilities_offered()
    capabilities.check(getattr(instrument, "REQUIRES", {}), offered)
    if not hasattr(instrument, "drive"):
        raise AttributeError(
            "instrument must define drive(scenario, services) to run in the local_process environment"
        )
    raw = instrument.drive(scenario, Services())
    raw.setdefault("_capabilities", offered)
    return raw


class _Environment:
    name = name
    capabilities = staticmethod(capabilities_offered)
    execute = staticmethod(execute)


ENVIRONMENT = _Environment()
