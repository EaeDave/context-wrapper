"""Contrato e cálculo de progresso estruturado dos jobs."""

from __future__ import annotations
import time

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any, Literal

StepState = Literal["pending", "running", "done", "error"]


@dataclass(frozen=True)
class ProgressStep:
    key: str
    label: str
    state: StepState
    elapsed_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass(frozen=True)
class ProgressUpdate:
    percent: float
    step: str
    step_label: str
    step_percent: float | None
    detail: str
    steps: tuple[ProgressStep, ...]
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "percent": self.percent,
            "step": self.step,
            "step_label": self.step_label,
            "step_percent": self.step_percent,
            "detail": self.detail,
            "steps": [step.to_dict() for step in self.steps],
            "elapsed_seconds": self.elapsed_seconds,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProgressUpdate:
        steps = tuple(
            ProgressStep(
                key=str(step["key"]),
                label=str(step["label"]),
                state=step["state"],
                elapsed_seconds=(
                    None
                    if step.get("elapsed_seconds") is None
                    else float(step["elapsed_seconds"])
                ),
            )
            for step in raw.get("steps", [])
        )
        return cls(
            percent=float(raw.get("percent", 0.0)),
            step=str(raw.get("step", "")),
            step_label=str(raw.get("step_label", "")),
            step_percent=(
                None
                if raw.get("step_percent") is None
                else float(raw["step_percent"])
            ),
            detail=str(raw.get("detail", "")),
            steps=steps,
            elapsed_seconds=float(raw.get("elapsed_seconds", 0.0)),
        )

    def failed(self, detail: str) -> ProgressUpdate:
        steps = tuple(
            replace(step, state="error") if step.key == self.step else step
            for step in self.steps
        )
        return replace(self, detail=detail, steps=steps)


@dataclass(frozen=True)
class StepSpec:
    key: str
    label: str
    weight: float


ProgressCallback = Callable[[ProgressUpdate], None]


class ProgressTracker:
    """Converte avanço por etapa em porcentagem geral ponderada."""

    def __init__(
        self,
        steps: Iterable[StepSpec],
        callback: ProgressCallback | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._steps = tuple(steps)
        if not self._steps:
            raise ValueError("Plano de progresso vazio")
        if any(step.weight <= 0 for step in self._steps):
            raise ValueError("Pesos de progresso devem ser positivos")
        self._total_weight = sum(step.weight for step in self._steps)
        self._callback = callback
        self._clock = clock
        self._started_at = clock()
        self._step_started_at: float | None = None
        self._step_elapsed: dict[str, float] = {}
        self._index = -1
        self._fraction = 0.0
        self._determinate = True
        self._detail = ""
        self._last: ProgressUpdate | None = None
        self._failed_steps: set[str] = set()
        self._failure_detail: str | None = None

    @property
    def last(self) -> ProgressUpdate | None:
        return self._last

    def start(
        self,
        key: str,
        detail: str | None = None,
        *,
        determinate: bool = True,
    ) -> None:
        index = next(
            (i for i, step in enumerate(self._steps) if step.key == key),
            None,
        )
        if index is None:
            raise KeyError(f"Etapa desconhecida: {key}")
        if index < self._index:
            raise ValueError(f"Etapa fora de ordem: {key}")
        now = self._clock()
        self._finish_active_timing(now)
        self._index = index
        self._step_started_at = now
        self._fraction = 0.0
        self._determinate = determinate
        self._detail = detail or self._steps[index].label
        self._emit(now)

    def update(self, fraction: float | None, detail: str | None = None) -> None:
        if self._index < 0:
            raise RuntimeError("Nenhuma etapa iniciada")
        if fraction is None:
            self._determinate = False
        else:
            self._fraction = min(1.0, max(0.0, fraction))
            self._determinate = True
        if detail is not None:
            self._detail = detail
        self._emit()

    def mark_current_error(self, detail: str) -> None:
        """Marca falha não fatal na etapa atual sem falhar o job inteiro."""
        if not 0 <= self._index < len(self._steps):
            raise RuntimeError("Nenhuma etapa iniciada")
        self._fraction = 1.0
        self._determinate = True
        self._failed_steps.add(self._steps[self._index].key)
        self._failure_detail = detail
        self._detail = detail
        self._emit()

    def finish(self, detail: str = "Concluído") -> None:
        now = self._clock()
        self._finish_active_timing(now)
        self._index = len(self._steps)
        self._step_started_at = None
        self._fraction = 1.0
        self._determinate = True
        self._detail = self._failure_detail or detail
        self._emit(now)

    def _finish_active_timing(self, now: float) -> None:
        if not 0 <= self._index < len(self._steps):
            return
        if self._step_started_at is None:
            return
        key = self._steps[self._index].key
        self._step_elapsed[key] = self._step_elapsed.get(key, 0.0) + max(
            now - self._step_started_at, 0.0
        )

    def _emit(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        finished = self._index >= len(self._steps)
        if finished:
            current = self._steps[-1]
            percent = 100.0
            step_percent: float | None = 100.0
        else:
            current = self._steps[max(self._index, 0)]
            completed_weight = sum(
                step.weight for step in self._steps[: max(self._index, 0)]
            )
            if self._index >= 0:
                completed_weight += current.weight * self._fraction
            percent = completed_weight / self._total_weight * 100.0
            step_percent = self._fraction * 100.0 if self._determinate else None

        def step_elapsed(index: int, key: str) -> float | None:
            elapsed = self._step_elapsed.get(key)
            if index == self._index and self._step_started_at is not None:
                elapsed = (elapsed or 0.0) + max(now - self._step_started_at, 0.0)
            if elapsed is None:
                return None
            return round(elapsed, 3)

        steps = tuple(
            ProgressStep(
                key=step.key,
                label=step.label,
                state=(
                    "error"
                    if step.key in self._failed_steps
                    else "done"
                    if finished or i < self._index
                    else "running"
                    if i == self._index
                    else "pending"
                ),
                elapsed_seconds=step_elapsed(i, step.key),
            )
            for i, step in enumerate(self._steps)
        )
        update = ProgressUpdate(
            percent=round(percent, 1),
            step=current.key,
            step_label=current.label,
            step_percent=(
                None if step_percent is None else round(step_percent, 1)
            ),
            detail=self._detail,
            steps=steps,
            elapsed_seconds=round(max(now - self._started_at, 0.0), 3),
        )
        self._last = update
        if self._callback is not None:
            self._callback(update)
