"""Minimal Prometheus-format metrics without a third-party dependency.

Counters and summaries (count/sum) cover what the bridge needs: upstream
latency/status, tool calls, cache hits/misses, idempotency replays, session
resets and tool-loop turns. The registry is process-wide and thread-safe.
"""

from __future__ import annotations

import threading


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    pairs = ",".join(
        f'{key}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for key, value in labels
    )
    return "{" + pairs + "}"


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._summaries: dict[
            tuple[str, tuple[tuple[str, str], ...]], list[float]
        ] = {}

    def increment(
        self,
        name: str,
        labels: dict[str, str] | None = None,
        value: float = 1.0,
    ) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
    ) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            entry = self._summaries.setdefault(key, [0.0, 0.0])
            entry[0] += 1
            entry[1] += value

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            summaries = {key: list(value) for key, value in self._summaries.items()}

        emitted_types: set[str] = set()
        for (name, labels), value in sorted(counters.items()):
            if name not in emitted_types:
                lines.append(f"# TYPE {name} counter")
                emitted_types.add(name)
            lines.append(f"{name}{_format_labels(labels)} {value:g}")

        emitted_types.clear()
        for (name, labels), (count, total) in sorted(summaries.items()):
            if name not in emitted_types:
                lines.append(f"# TYPE {name} summary")
                emitted_types.add(name)
            lines.append(f"{name}_count{_format_labels(labels)} {count:g}")
            lines.append(f"{name}_sum{_format_labels(labels)} {total:g}")

        return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._summaries.clear()


metrics = MetricsRegistry()


__all__ = ["MetricsRegistry", "metrics"]
