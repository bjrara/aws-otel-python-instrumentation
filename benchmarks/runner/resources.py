# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""CPU and memory sampling around a measured block.

CPU comes from getrusage rather than wall clock so it is not polluted by the receiver thread waiting on
recvfrom. RSS is sampled by a poller because peak matters more than the value at any one instant.
"""
import resource
import threading
import time
from dataclasses import dataclass
from typing import Optional

import psutil


@dataclass
class ResourceUsage:
    cpu_user_s: float
    cpu_sys_s: float
    peak_rss_bytes: int
    rss_delta_bytes: int


class ResourceSampler:
    """Context manager collecting CPU time and peak RSS for the work inside it."""

    def __init__(self, interval_s: float = 0.01) -> None:
        self._interval = interval_s
        self._process = psutil.Process()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._peak_rss = 0
        self._start_rss = 0
        self._start_cpu = (0.0, 0.0)
        self.usage: Optional[ResourceUsage] = None

    def __enter__(self) -> "ResourceSampler":
        usage = resource.getrusage(resource.RUSAGE_SELF)
        self._start_cpu = (usage.ru_utime, usage.ru_stime)
        self._start_rss = self._process.memory_info().rss
        self._peak_rss = self._start_rss
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        after = resource.getrusage(resource.RUSAGE_SELF)
        self.usage = ResourceUsage(
            cpu_user_s=after.ru_utime - self._start_cpu[0],
            cpu_sys_s=after.ru_stime - self._start_cpu[1],
            peak_rss_bytes=self._peak_rss,
            rss_delta_bytes=self._process.memory_info().rss - self._start_rss,
        )

    def _sample(self) -> None:
        while not self._stop.is_set():
            try:
                rss = self._process.memory_info().rss
            except psutil.Error:
                return
            self._peak_rss = max(self._peak_rss, rss)
            time.sleep(self._interval)
