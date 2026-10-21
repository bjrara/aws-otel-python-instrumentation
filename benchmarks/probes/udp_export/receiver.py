# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""A UDP sink standing in for the Lambda X-Ray daemon.

The receive buffer is pinned so delivery rates are comparable between runs. Loss here is loss at the
receiver's socket buffer, which is the mechanism the PR's known limitation describes; the absolute rate
depends on the buffer size and so does not predict the real daemon.
"""
import socket
import threading
from typing import Optional

# Linux default for a UDP socket; pinned explicitly so the number does not drift with the host
DEFAULT_RCVBUF = 212_992


class UdpReceiver:
    """Counts datagrams and bytes arriving on a loopback port."""

    def __init__(self, rcvbuf: int = DEFAULT_RCVBUF, host: str = "127.0.0.1") -> None:
        self._requested_rcvbuf = rcvbuf
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self._socket.bind((host, 0))
        self._socket.settimeout(0.2)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.datagrams = 0
        self.bytes = 0

    @property
    def endpoint(self) -> str:
        host, port = self._socket.getsockname()
        return f"{host}:{port}"

    @property
    def requested_rcvbuf(self) -> int:
        return self._requested_rcvbuf

    @property
    def effective_rcvbuf(self) -> int:
        """What the kernel actually granted.

        setsockopt silently caps at net.core.rmem_max (often 212,992), so asking for a large buffer is
        not the same as having one. When this comes back far below the request, a low delivery rate says
        more about the buffer than about the code under test.
        """
        return self._socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)

    def __enter__(self) -> "UdpReceiver":
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._socket.close()

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._socket.recvfrom(65_535)
            except socket.timeout:
                continue
            except OSError:
                return
            self.datagrams += 1
            self.bytes += len(data)
