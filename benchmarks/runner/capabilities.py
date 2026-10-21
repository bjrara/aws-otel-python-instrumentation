# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""What an environment can represent, and what an instrument needs it to represent.

This exists because a measurement taken in an environment that cannot reproduce production is worse
than no measurement: it returns a plausible number. macOS caps UDP datagrams at 9216 bytes, so an
exporter that assumes 65,507 fails there no matter what the code under test does -- and the run still
looks like it completed.

Capabilities live with the persisted environment rather than with the throwaway instrument, so the
lesson survives even when the instrument that found it is regenerated and discarded.
"""
from dataclasses import dataclass
from typing import Dict, Mapping

# Well-known capability keys. An environment may publish others; an instrument may require others.
UDP_DATAGRAM_BYTES = "udp_datagram_bytes"  # largest datagram the environment will carry
REAL_TELEMETRY_BACKEND = "real_telemetry_backend"  # spans reach the actual receiver, not a stand-in
PROCESS_RESOURCE_SAMPLING = "process_resource_sampling"  # CPU and RSS of the process under test
CONFIGURABLE_RECEIVE_BUFFER = "configurable_receive_buffer"  # receiver SO_RCVBUF can be pinned


class CapabilityError(RuntimeError):
    """The environment cannot represent what the instrument needs measured."""


@dataclass
class Unmet:
    key: str
    required: object
    offered: object

    def __str__(self) -> str:
        offered = "absent" if self.offered is None else self.offered
        return f"{self.key}: need {self.required!r}, environment offers {offered!r}"


def check(required: Mapping[str, object], offered: Mapping[str, object]) -> None:
    """Raise unless every requirement is met. Numbers compare as minimums, everything else as equality."""
    unmet = []
    for key, want in required.items():
        have = offered.get(key)
        if have is None:
            unmet.append(Unmet(key, want, None))
        elif isinstance(want, bool) or isinstance(have, bool):
            if bool(have) != bool(want):
                unmet.append(Unmet(key, want, have))
        elif isinstance(want, (int, float)) and isinstance(have, (int, float)):
            if have < want:
                unmet.append(Unmet(key, want, have))
        elif have != want:
            unmet.append(Unmet(key, want, have))

    if unmet:
        raise CapabilityError(
            "environment cannot support this instrument: " + "; ".join(str(u) for u in unmet)
        )


def describe(offered: Mapping[str, object]) -> Dict[str, object]:
    """Capabilities as plain metrics, so a report records the environment it was measured in."""
    return {f"env_{key}": value for key, value in offered.items()}
