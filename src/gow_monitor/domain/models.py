from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ObjectiveDirection(str, Enum):
    UNKNOWN = "unknown"
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"


class RunState(str, Enum):
    UNKNOWN = "unknown"
    WAITING = "waiting"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    RESUMING = "resuming"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class RunReference:
    run_id: str
    run_root: Path
    direction: ObjectiveDirection = ObjectiveDirection.UNKNOWN
    problem_id: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id cannot be empty")
        object.__setattr__(self, "run_root", Path(self.run_root).expanduser())


@dataclass(frozen=True, slots=True)
class EvaluationPoint:
    evaluation: int
    candidate_id: str | None
    status: str
    objective: float | None
    best_so_far: float | None
    mean_so_far: float | None
    median_so_far: float | None
    generation_id: int | None = None
    parameters: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if self.evaluation < 1:
            raise ValueError("evaluation must be at least 1")
        if self.generation_id is not None and self.generation_id < 0:
            raise ValueError("generation_id cannot be negative")
        for name, value in self.parameters:
            if not name:
                raise ValueError("parameter names cannot be empty")
            if not isinstance(value, float):
                raise TypeError("parameter values must be floats")

    @property
    def is_valid(self) -> bool:
        return self.status == "ok" and self.objective is not None

    @property
    def parameter_map(self) -> dict[str, float]:
        return dict(self.parameters)


@dataclass(frozen=True, slots=True)
class PopulationDiversityPoint:
    """Two complementary diversity observations for one generation."""

    generation_id: int
    evaluation: int
    spread: float
    ellipse_area: float
    population_size: int
    active_dimensions: int

    def __post_init__(self) -> None:
        if self.generation_id < 0:
            raise ValueError("generation_id cannot be negative")
        if self.evaluation < 1:
            raise ValueError("evaluation must be at least 1")
        if self.population_size < 0:
            raise ValueError("population_size cannot be negative")
        if self.active_dimensions < 0:
            raise ValueError("active_dimensions cannot be negative")

    @property
    def diversity(self) -> float:
        """Backward-compatible alias for the marginal spread metric."""

        return self.spread


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    reference: RunReference
    state: RunState
    evaluation_count: int
    failed_evaluations: int
    result_sources: int
    successful_evaluations: int = 0
    best_objective: float | None = None
    latest_objective: float | None = None
    mean_objective: float | None = None
    median_objective: float | None = None
    best_candidate_id: str | None = None
    planned_evaluations: int | None = None
    completed_generations: int | None = None
    history_is_sampled: bool = False
    population_diversity: tuple[PopulationDiversityPoint, ...] = ()
    run_started_at: float | None = None
    run_finished_at: float | None = None
    artifact_mode: str = "legacy"
    quality_metrics_complete: bool = True
    fireworks_total: int = 0
    fireworks_completed: int = 0
    fireworks_fizzled: int = 0
    fireworks_archived: int = 0
    launches_total: int = 0
    launch_manifest_generations: int = 0
    archived_candidate_generations: int = 0
    archived_launcher_generations: int = 0
    final_launcher_archive: bool = False
    launcher_inspection_errors: int = 0

    def __post_init__(self) -> None:
        if self.evaluation_count < 0:
            raise ValueError("evaluation_count cannot be negative")
        if self.failed_evaluations < 0:
            raise ValueError("failed_evaluations cannot be negative")
        if self.successful_evaluations < 0:
            raise ValueError("successful_evaluations cannot be negative")
        if self.failed_evaluations > self.evaluation_count:
            raise ValueError("failed_evaluations cannot exceed evaluation_count")
        if self.successful_evaluations > self.evaluation_count:
            raise ValueError("successful_evaluations cannot exceed evaluation_count")
        if self.result_sources < 0:
            raise ValueError("result_sources cannot be negative")
        for field_name, value in (
            ("fireworks_total", self.fireworks_total),
            ("fireworks_completed", self.fireworks_completed),
            ("fireworks_fizzled", self.fireworks_fizzled),
            ("fireworks_archived", self.fireworks_archived),
            ("launches_total", self.launches_total),
            ("launch_manifest_generations", self.launch_manifest_generations),
            ("archived_candidate_generations", self.archived_candidate_generations),
            ("archived_launcher_generations", self.archived_launcher_generations),
            ("launcher_inspection_errors", self.launcher_inspection_errors),
        ):
            if value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if (
            self.planned_evaluations is not None
            and self.planned_evaluations < 0
        ):
            raise ValueError("planned_evaluations cannot be negative")
        if (
            self.completed_generations is not None
            and self.completed_generations < 0
        ):
            raise ValueError("completed_generations cannot be negative")

        for field_name, value in (
            ("run_started_at", self.run_started_at),
            ("run_finished_at", self.run_finished_at),
        ):
            if value is None:
                continue
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(
                    f"{field_name} must be a finite non-negative timestamp"
                )

        if (
            self.run_started_at is not None
            and self.run_finished_at is not None
            and self.run_finished_at < self.run_started_at
        ):
            raise ValueError(
                "run_finished_at cannot precede run_started_at"
            )

    @property
    def success_rate(self) -> float:
        if self.evaluation_count == 0:
            return 0.0
        return self.successful_evaluations / self.evaluation_count

    @property
    def failure_rate(self) -> float:
        if self.evaluation_count == 0:
            return 0.0
        return self.failed_evaluations / self.evaluation_count

@dataclass(frozen=True, slots=True)
class GpuResourceSnapshot:
    """One GPU device with complete, partial or detection-only telemetry.

    A missing utilization value means that the backend cannot observe the
    metric. It must never be converted into an artificial zero percent value.

    ``memory_used_bytes`` and ``memory_total_bytes`` represent the generic
    memory figures reported by vendor-oriented backends such as nvidia-smi.

    Windows performance counters may instead expose dedicated, shared and
    committed memory independently. Those values are therefore represented
    explicitly and are not mislabeled as physical VRAM capacity.
    """

    name: str
    utilization_percent: float | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    backend: str = "unknown"
    device_id: str | None = None
    vendor: str | None = None
    dedicated_memory_used_bytes: int | None = None
    shared_memory_used_bytes: int | None = None
    committed_memory_bytes: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("GPU name cannot be empty")

        if not self.backend.strip():
            raise ValueError("GPU backend cannot be empty")

        if self.device_id is not None and not self.device_id.strip():
            raise ValueError("GPU device_id cannot be empty when provided")

        if self.vendor is not None and not self.vendor.strip():
            raise ValueError("GPU vendor cannot be empty when provided")

        if (
            self.utilization_percent is not None
            and not 0.0 <= self.utilization_percent <= 100.0
        ):
            raise ValueError("GPU utilization must be between 0 and 100")

        memory_values = {
            "memory_used_bytes": self.memory_used_bytes,
            "memory_total_bytes": self.memory_total_bytes,
            "dedicated_memory_used_bytes": (
                self.dedicated_memory_used_bytes
            ),
            "shared_memory_used_bytes": self.shared_memory_used_bytes,
            "committed_memory_bytes": self.committed_memory_bytes,
        }

        for field_name, value in memory_values.items():
            if value is not None and value < 0:
                raise ValueError(
                    f"{field_name} cannot be negative"
                )

        if (
            self.memory_used_bytes is not None
            and self.memory_total_bytes is not None
            and self.memory_used_bytes > self.memory_total_bytes
        ):
            raise ValueError("GPU memory used cannot exceed total memory")

    @property
    def has_utilization(self) -> bool:
        return self.utilization_percent is not None

    @property
    def has_memory_telemetry(self) -> bool:
        return any(
            value is not None
            for value in (
                self.memory_used_bytes,
                self.memory_total_bytes,
                self.dedicated_memory_used_bytes,
                self.shared_memory_used_bytes,
                self.committed_memory_bytes,
            )
        )

    @property
    def telemetry_level(self) -> str:
        if (
            self.utilization_percent is not None
            and self.memory_used_bytes is not None
            and self.memory_total_bytes is not None
        ):
            return "full"

        if self.has_utilization or self.has_memory_telemetry:
            return "partial"

        return "detected"


@dataclass(frozen=True, slots=True)
class SystemResourceSnapshot:
    captured_at: float
    cpu_percent: float
    per_core_percent: tuple[float, ...]
    physical_cores: int | None
    logical_cores: int
    active_logical_cores: int
    active_core_threshold_percent: float
    memory_percent: float
    memory_used_bytes: int
    memory_total_bytes: int
    gpus: tuple[GpuResourceSnapshot, ...] = ()
    gpu_status: str = "No supported GPU telemetry backend detected"

    def __post_init__(self) -> None:
        if self.captured_at < 0:
            raise ValueError("captured_at cannot be negative")
        if not 0.0 <= self.cpu_percent <= 100.0:
            raise ValueError("CPU utilization must be between 0 and 100")
        if any(not 0.0 <= value <= 100.0 for value in self.per_core_percent):
            raise ValueError("Per-core utilization must be between 0 and 100")
        if self.physical_cores is not None and self.physical_cores < 1:
            raise ValueError("physical_cores must be positive when available")
        if self.logical_cores < 1:
            raise ValueError("logical_cores must be positive")
        if not 0 <= self.active_logical_cores <= self.logical_cores:
            raise ValueError("active_logical_cores must be within logical core count")
        if not 0.0 <= self.active_core_threshold_percent <= 100.0:
            raise ValueError("active core threshold must be between 0 and 100")
        if not 0.0 <= self.memory_percent <= 100.0:
            raise ValueError("memory utilization must be between 0 and 100")
        if self.memory_used_bytes < 0 or self.memory_total_bytes < 0:
            raise ValueError("memory byte counts cannot be negative")
        if self.memory_used_bytes > self.memory_total_bytes:
            raise ValueError("memory used cannot exceed total memory")

    @property
    def primary_gpu(self) -> GpuResourceSnapshot | None:
        if not self.gpus:
            return None

        def telemetry_score(
            gpu: GpuResourceSnapshot,
        ) -> tuple[bool, float, bool, int]:
            utilization = (
                gpu.utilization_percent
                if gpu.utilization_percent is not None
                else -1.0
            )
            memory_used = (
                gpu.memory_used_bytes
                if gpu.memory_used_bytes is not None
                else -1
            )

            return (
                gpu.utilization_percent is not None,
                utilization,
                gpu.memory_used_bytes is not None,
                memory_used,
            )

        return max(self.gpus, key=telemetry_score)
