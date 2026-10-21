# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Host preconditions for measuring UDP export.

The distro assumes a 65,507 byte datagram ceiling, which is the IPv4 maximum and what Linux and Lambda
allow. macOS defaults net.inet.udp.maxdgram to 9216, so every datagram the splitter produces fails with
EMSGSIZE no matter what the code under test does -- and the run still looks like it completed, reporting
0% delivery on both refs. That reads as a result rather than a broken measurement, so the probe checks
the host up front and refuses to report numbers it cannot stand behind.
"""
import socket
from functools import lru_cache

# What the code under test assumes: 65,535 - 8 byte UDP header - 20 byte IP header
PRODUCTION_MAX_DATAGRAM = 65_507

REMEDY = (
    "Tier A needs a host that allows 65,507 byte datagrams. Either run it on Linux (or in a Linux "
    "container), or raise the limit on macOS with: sudo sysctl -w net.inet.udp.maxdgram=65535"
)


@lru_cache(maxsize=1)
def max_sendable_datagram() -> int:
    """Largest datagram this host will accept on loopback, found by binary search."""
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        receiver.bind(("127.0.0.1", 0))
        target = receiver.getsockname()
        low, high = 1, PRODUCTION_MAX_DATAGRAM
        while low < high:
            mid = (low + high + 1) // 2
            try:
                sender.sendto(b"\0" * mid, target)
                low = mid
            except OSError:
                high = mid - 1
        return low
    finally:
        sender.close()
        receiver.close()


def check(allow_undersized: bool = False) -> None:
    """Raise unless the host can carry a production-sized datagram."""
    limit = max_sendable_datagram()
    if limit >= PRODUCTION_MAX_DATAGRAM or allow_undersized:
        return
    raise RuntimeError(
        f"host accepts datagrams up to {limit:,} bytes but the code under test assumes "
        f"{PRODUCTION_MAX_DATAGRAM:,}; measurements here would reflect the host, not the change. {REMEDY} "
        f"(set params.allow_undersized_host: true to measure anyway)"
    )
