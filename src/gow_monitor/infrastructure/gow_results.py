from __future__ import annotations

import heapq
import json
import math
import re
import statistics
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gow_monitor.domain import (
    EvaluationPoint,
    ObjectiveDirection,
    PopulationDiversityPoint,
    RunReference,
    RunSnapshot,
    RunState,
)
from gow_monitor.domain.diversity import confidence_ellipse_area


@dataclass(frozen=True, slots=True)
class GowPathResolution:
    selected_path: Path
    results_root: Path
    runs_root: Path
    direct_run_root: Path | None = None


class _RunningMedian:
    """Track a numeric median in O(log n) per inserted value."""

    def __init__(self) -> None:
        self._lower: list[float] = []
        self._upper: list[float] = []

    def add(self, value: float) -> None:
        if not self._lower or value <= -self._lower[0]:
            heapq.heappush(self._lower, -value)
        else:
            heapq.heappush(self._upper, value)

        if len(self._lower) > len(self._upper) + 1:
            heapq.heappush(self._upper, -heapq.heappop(self._lower))
        elif len(self._upper) > len(self._lower):
            heapq.heappush(self._lower, -heapq.heappop(self._upper))

    @property
    def value(self) -> float | None:
        if not self._lower:
            return None
        if len(self._lower) == len(self._upper):
            return (-self._lower[0] + self._upper[0]) / 2.0
        return -self._lower[0]


@dataclass(slots=True)
class _GenerationMoments:
    count: int = 0
    evaluation: int = 0
    sums: dict[str, float] = field(default_factory=dict)
    cross_sums: dict[tuple[str, str], float] = field(
        default_factory=dict
    )

    def add(
        self,
        *,
        evaluation: int,
        parameters: dict[str, float],
    ) -> None:
        self.count += 1
        self.evaluation = max(self.evaluation, evaluation)
        names = tuple(sorted(parameters))

        for name in names:
            self.sums[name] = (
                self.sums.get(name, 0.0) + parameters[name]
            )

        for left_index, left_name in enumerate(names):
            left_value = parameters[left_name]
            for right_name in names[left_index:]:
                key = (left_name, right_name)
                self.cross_sums[key] = (
                    self.cross_sums.get(key, 0.0)
                    + left_value * parameters[right_name]
                )


@dataclass(slots=True)
class _CachedShardRun:
    shard_fingerprint: tuple[tuple[str, int, int], ...]
    state_fingerprint: tuple[tuple[str, int, int], ...]
    accumulator: _ShardRunAccumulator
    source_count: int
    snapshot: RunSnapshot
    history: tuple[EvaluationPoint, ...]


@dataclass(frozen=True, slots=True)
class _LaunchTelemetry:
    manifest_generations: int = 0
    fireworks_total: int = 0
    fireworks_completed: int = 0
    fireworks_fizzled: int = 0
    fireworks_archived: int = 0
    launches_total: int = 0
    inspection_errors: int = 0
    archived_candidate_generations: int = 0
    archived_launcher_generations: int = 0
    final_launcher_archive: bool = False


@dataclass(slots=True)
class _CachedLaunchTelemetry:
    directory_fingerprint: tuple[tuple[str, int], ...]
    manifest_fingerprint: tuple[tuple[str, int, int], ...]
    report_fingerprint: tuple[tuple[str, int, int], ...]
    seen_fw_ids: set[str]
    telemetry: _LaunchTelemetry


class _ShardRunAccumulator:
    _MAX_HISTORY_POINTS = 4096
    _RECENT_HISTORY_POINTS = 100

    def __init__(
        self,
        run: RunReference,
    ) -> None:
        self.run = run
        self.evaluation_count = 0
        self.failed_evaluations = 0
        self.successful_evaluations = 0
        self.objective_sum = 0.0
        self.latest_objective: float | None = None
        self.best_objective: float | None = None
        self.best_candidate_id: str | None = None
        self.run_started_at: float | None = None
        self.run_finished_at: float | None = None
        self.median = _RunningMedian()
        self.sample_step = 1
        self.sampled_history: list[EvaluationPoint] = []
        self.recent_history: deque[EvaluationPoint] = deque(
            maxlen=self._RECENT_HISTORY_POINTS
        )
        # Generation shards are canonical, de-duplicated GOW artifacts.
        # Do not retain every candidate id in memory: that set becomes
        # prohibitive for 10M-100M evaluation campaigns.
        self.parameterized_count = 0
        self.parameter_presence: dict[str, int] = {}
        self.parameter_minima: dict[str, float] = {}
        self.parameter_maxima: dict[str, float] = {}
        self.generations: dict[int, _GenerationMoments] = {}

    def consume(
        self,
        payload: dict[str, Any],
        *,
        source: Path,
    ) -> None:
        # Shards are finalized once per generation by GOW, so duplicate
        # suppression belongs upstream. Avoid an O(evaluations) identity set.
        self.evaluation_count += 1
        evaluation = self.evaluation_count
        status = GowFilesystemRunReader._fitness_status(payload)
        objective = (
            GowFilesystemRunReader._fitness_objective(payload)
            if status == "ok"
            else None
        )
        candidate_id = GowFilesystemRunReader._candidate_id(payload)
        generation_id = GowFilesystemRunReader._generation_id(payload)
        started_at = GowFilesystemRunReader._timestamp_value(
            payload.get("started_at")
        )
        finished_at = GowFilesystemRunReader._timestamp_value(
            payload.get("finished_at")
        )

        if started_at is not None:
            self.run_started_at = (
                started_at
                if self.run_started_at is None
                else min(self.run_started_at, started_at)
            )
        if finished_at is not None:
            self.run_finished_at = (
                finished_at
                if self.run_finished_at is None
                else max(self.run_finished_at, finished_at)
            )

        if status != "ok":
            self.failed_evaluations += 1

        if objective is not None:
            self.successful_evaluations += 1
            self.objective_sum += objective
            self.latest_objective = objective
            self.median.add(objective)

            if self.best_objective is None:
                self.best_objective = objective
                self.best_candidate_id = candidate_id
            elif self.run.direction is ObjectiveDirection.MAXIMIZE:
                if objective > self.best_objective:
                    self.best_objective = objective
                    self.best_candidate_id = candidate_id
            elif objective < self.best_objective:
                self.best_objective = objective
                self.best_candidate_id = candidate_id

        mean_so_far = (
            self.objective_sum / self.successful_evaluations
            if self.successful_evaluations
            else None
        )
        point = EvaluationPoint(
            evaluation=evaluation,
            candidate_id=candidate_id,
            status=status,
            objective=objective,
            best_so_far=self.best_objective,
            mean_so_far=mean_so_far,
            median_so_far=self.median.value,
            generation_id=generation_id,
        )
        self.recent_history.append(point)

        if evaluation == 1 or evaluation % self.sample_step == 0:
            self.sampled_history.append(point)
            if len(self.sampled_history) > self._MAX_HISTORY_POINTS:
                self.sampled_history = self.sampled_history[::2]
                self.sample_step *= 2

        parameters = dict(
            GowFilesystemRunReader._parameter_items(payload)
        )
        if generation_id is not None and parameters:
            self._consume_parameters(
                evaluation=evaluation,
                generation_id=generation_id,
                parameters=parameters,
            )

    def _consume_parameters(
        self,
        *,
        evaluation: int,
        generation_id: int,
        parameters: dict[str, float],
    ) -> None:
        self.parameterized_count += 1

        for name, value in parameters.items():
            self.parameter_presence[name] = (
                self.parameter_presence.get(name, 0) + 1
            )
            self.parameter_minima[name] = min(
                value,
                self.parameter_minima.get(name, value),
            )
            self.parameter_maxima[name] = max(
                value,
                self.parameter_maxima.get(name, value),
            )

        generation = self.generations.setdefault(
            generation_id,
            _GenerationMoments(),
        )
        generation.add(
            evaluation=evaluation,
            parameters=parameters,
        )

    def history(self) -> tuple[EvaluationPoint, ...]:
        merged = {
            point.evaluation: point
            for point in self.sampled_history
        }
        merged.update(
            {
                point.evaluation: point
                for point in self.recent_history
            }
        )
        if self.recent_history:
            latest = self.recent_history[-1]
            merged[latest.evaluation] = latest
        return tuple(merged[index] for index in sorted(merged))

    def diversity(self) -> tuple[PopulationDiversityPoint, ...]:
        if self.parameterized_count == 0:
            return ()

        common_names = tuple(
            sorted(
                name
                for name, count in self.parameter_presence.items()
                if count == self.parameterized_count
            )
        )
        active_names = tuple(
            name
            for name in common_names
            if not math.isclose(
                self.parameter_minima[name],
                self.parameter_maxima[name],
                rel_tol=0.0,
                abs_tol=1e-15,
            )
        )
        observations: list[PopulationDiversityPoint] = []

        for generation_id in sorted(self.generations):
            generation = self.generations[generation_id]
            covariance = self._normalized_covariance(
                generation,
                active_names,
            )
            spread = sum(
                math.sqrt(max(0.0, covariance[index][index]))
                for index in range(len(covariance))
            )
            observations.append(
                PopulationDiversityPoint(
                    generation_id=generation_id,
                    evaluation=generation.evaluation,
                    spread=spread,
                    ellipse_area=confidence_ellipse_area(covariance),
                    population_size=generation.count,
                    active_dimensions=len(active_names),
                )
            )

        return tuple(observations)

    def _normalized_covariance(
        self,
        generation: _GenerationMoments,
        active_names: tuple[str, ...],
    ) -> tuple[tuple[float, ...], ...]:
        if generation.count < 2 or not active_names:
            return ()

        denominator = generation.count - 1
        rows: list[tuple[float, ...]] = []

        for left_name in active_names:
            row: list[float] = []
            left_range = (
                self.parameter_maxima[left_name]
                - self.parameter_minima[left_name]
            )
            for right_name in active_names:
                right_range = (
                    self.parameter_maxima[right_name]
                    - self.parameter_minima[right_name]
                )
                key = tuple(sorted((left_name, right_name)))
                cross_sum = generation.cross_sums.get(key, 0.0)
                raw_covariance = (
                    cross_sum
                    - (
                        generation.sums[left_name]
                        * generation.sums[right_name]
                        / generation.count
                    )
                ) / denominator
                row.append(
                    raw_covariance / (left_range * right_range)
                )
            rows.append(tuple(row))

        return tuple(rows)


class GowFilesystemRunReader:
    """Read GOW artifacts from an output root or any nested folder."""

    _shard_cache: dict[Path, _CachedShardRun] = {}
    _shard_cache_lock = threading.Lock()
    _launch_cache: dict[Path, _CachedLaunchTelemetry] = {}
    _launch_cache_lock = threading.Lock()
    _AUTO_SUMMARY_THRESHOLD = 1_000_000

    def __init__(
        self,
        selected_path: str | Path,
        *,
        data_mode: str = "auto",
        summary_threshold: int = _AUTO_SUMMARY_THRESHOLD,
    ) -> None:
        normalized_mode = str(data_mode).strip().lower()
        if normalized_mode not in {"auto", "summary", "full"}:
            raise ValueError("data_mode must be one of: auto, summary, full")
        if summary_threshold < 1:
            raise ValueError("summary_threshold must be positive")
        self.data_mode = normalized_mode
        self.summary_threshold = int(summary_threshold)
        self.resolution = self.resolve_selected_path(selected_path)
        self.selected_path = self.resolution.selected_path
        self.results_root = self.resolution.results_root
        self.runs_root = self.resolution.runs_root
        self.direct_run_root = self.resolution.direct_run_root

    @classmethod
    def resolve_selected_path(
        cls,
        selected_path: str | Path,
    ) -> GowPathResolution:
        selected = Path(selected_path).expanduser().resolve()

        if not selected.exists():
            raise FileNotFoundError(
                f"The selected path does not exist: {selected}"
            )
        if not selected.is_dir():
            raise NotADirectoryError(
                f"The selected path is not a directory: {selected}"
            )

        for current in (selected, *selected.parents):
            child_runs = current / "runs"
            if child_runs.is_dir():
                return GowPathResolution(
                    selected_path=selected,
                    results_root=current,
                    runs_root=child_runs,
                )

            if current.name.lower() == "runs":
                return GowPathResolution(
                    selected_path=selected,
                    results_root=current.parent,
                    runs_root=current,
                )

            if current.parent.name.lower() == "runs":
                return GowPathResolution(
                    selected_path=selected,
                    results_root=current.parent.parent,
                    runs_root=current.parent,
                    direct_run_root=current,
                )

        inferred_run = cls._infer_run_root_from_artifacts(selected)
        if inferred_run is not None:
            return GowPathResolution(
                selected_path=selected,
                results_root=inferred_run.parent,
                runs_root=inferred_run.parent,
                direct_run_root=inferred_run,
            )

        return GowPathResolution(
            selected_path=selected,
            results_root=selected,
            runs_root=selected / "runs",
        )

    @classmethod
    def _infer_run_root_from_artifacts(
        cls,
        selected: Path,
    ) -> Path | None:
        for current in (selected, *selected.parents):
            if cls._looks_like_run_root(current):
                return current
        return None

    @staticmethod
    def _looks_like_run_root(path: Path) -> bool:
        if not path.is_dir():
            return False
        if (path / "results.jsonl").is_file():
            return True
        if (path / "summary.json").is_file():
            return True
        if (path / "summary.jsonl").is_file():
            return True
        generation_root = path / "generations"
        if generation_root.is_dir():
            try:
                if next(generation_root.glob("g*.jsonl"), None) is not None:
                    return True
                if next(generation_root.glob("g*.launches.json"), None) is not None:
                    return True
            except OSError:
                pass
        if (path / "archives").is_dir():
            return True
        try:
            return next(path.rglob("result.json"), None) is not None
        except OSError:
            return False

    def discover_runs(self) -> tuple[RunReference, ...]:
        if self.direct_run_root is not None:
            run_roots = (self.direct_run_root,)
        elif self.runs_root.is_dir():
            run_roots = tuple(
                path
                for path in self.runs_root.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            )
        elif self._looks_like_run_root(self.selected_path):
            run_roots = (self.selected_path,)
        else:
            return ()

        references = [
            self._build_reference(run_root)
            for run_root in run_roots
        ]
        return tuple(
            sorted(references, key=lambda item: item.run_id.lower())
        )

    def state_of(self, run: RunReference) -> RunState:
        return self.snapshot_of(run).state

    @staticmethod
    def _checkpoint_status(
        run: RunReference,
    ) -> str | None:
        manifest_path = (
            run.run_root
            / "checkpoint"
            / "manifest.json"
        )

        payload = GowFilesystemRunReader._read_json_object(
            manifest_path
        )

        raw_status = payload.get("status")

        if not isinstance(raw_status, str):
            return None

        status = raw_status.strip().lower()

        return status or None

    @classmethod
    def _state_from_artifacts(
        cls,
        run: RunReference,
        *,
        has_records: bool,
    ) -> RunState:
        if not run.run_root.is_dir():
            return RunState.UNKNOWN

        checkpoint_status = cls._checkpoint_status(run)

        # A completed checkpoint is authoritative.
        if checkpoint_status == "completed":
            return RunState.COMPLETED

        control_root = run.run_root / "control"

        pause_request_path = (
            control_root
            / "pause.request.json"
        )

        pause_ack_path = (
            control_root
            / "pause.ack.json"
        )

        # The request still exists until GOW reaches a safe generation
        # boundary, persists its checkpoint and acknowledges the pause.
        if pause_request_path.is_file():
            return RunState.PAUSE_REQUESTED

        # The acknowledgement may remain on disk after a later resume.
        # Therefore ACK alone is not sufficient: the checkpoint must
        # still explicitly state that the run is paused.
        if (
            checkpoint_status == "paused"
            and pause_ack_path.is_file()
        ):
            return RunState.PAUSED

        # Checkpoint-aware runs use the manifest as their authoritative
        # lifecycle signal.
        if checkpoint_status == "running":
            return RunState.RUNNING

        if not has_records:
            return RunState.WAITING

        # Backward compatibility for runs created before the checkpoint
        # protocol existed.
        if (run.run_root / "results.jsonl").is_file():
            return RunState.COMPLETED

        return RunState.RUNNING

    @classmethod
    def _state_from_records(
        cls,
        run: RunReference,
        records: dict[str, dict[str, Any]],
    ) -> RunState:
        return cls._state_from_artifacts(
            run,
            has_records=bool(records),
        )

    def snapshot_of(self, run: RunReference) -> RunSnapshot:
        return self.snapshot_and_history_of(run)[0]

    def history_of(self, run: RunReference) -> tuple[EvaluationPoint, ...]:
        return self.snapshot_and_history_of(run)[1]

    @staticmethod
    def _summary_rows(run_root: Path) -> tuple[dict[str, Any], ...]:
        path = run_root / "summary.jsonl"
        rows: list[dict[str, Any]] = []
        for _line_number, payload in GowFilesystemRunReader._read_jsonl(path):
            rows.append(payload)
        return tuple(rows)

    def _should_use_summary_mode(self, run: RunReference) -> bool:
        summary_path = run.run_root / "summary.jsonl"
        if not summary_path.is_file():
            return False
        if self.data_mode == "summary":
            return True
        if self.data_mode == "full":
            return False
        metadata = self._metadata_for(run.run_id, run.run_root)
        planned = self._nonnegative_int(metadata.get("max_evaluations"))
        done = self._nonnegative_int(metadata.get("evaluations_done"))
        scale = planned if planned is not None else (done or 0)
        return scale >= self.summary_threshold

    @staticmethod
    def _is_htc_artifact_run(run_root: Path) -> bool:
        return any(
            path.exists()
            for path in (
                run_root / "launchers",
                run_root / "generations",
                run_root / "archives",
                run_root / "summary.jsonl",
            )
        )

    def _launch_telemetry(self, run_root: Path) -> _LaunchTelemetry:
        generation_root = run_root / "generations"
        archives_root = run_root / "archives"
        report_root = archives_root / "manifests"
        candidates_root = archives_root / "candidates"
        launchers_root = archives_root / "launchers"

        directory_fingerprint_items: list[tuple[str, int]] = []
        for directory in (
            generation_root,
            report_root,
            candidates_root,
            launchers_root,
        ):
            try:
                stat = directory.stat()
            except OSError:
                continue
            directory_fingerprint_items.append((str(directory), stat.st_mtime_ns))
        directory_fingerprint = tuple(directory_fingerprint_items)
        cache_key = run_root.resolve()
        with self._launch_cache_lock:
            cached = self._launch_cache.get(cache_key)
        if (
            cached is not None
            and cached.directory_fingerprint == directory_fingerprint
        ):
            return cached.telemetry

        def _fingerprints(paths: tuple[Path, ...]) -> tuple[tuple[str, int, int], ...]:
            rows: list[tuple[str, int, int]] = []
            for path in paths:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rows.append((str(path), stat.st_size, stat.st_mtime_ns))
            return tuple(rows)

        try:
            manifest_paths = tuple(
                sorted(generation_root.glob("g*.launches.json"))
            )
        except OSError:
            manifest_paths = ()
        try:
            report_paths = tuple(
                sorted(report_root.glob("g*.archive.json"))
            )
        except OSError:
            report_paths = ()

        manifest_fingerprint = _fingerprints(manifest_paths)
        report_fingerprint = _fingerprints(report_paths)

        exact_cache = (
            cached is not None
            and cached.manifest_fingerprint == manifest_fingerprint
            and cached.report_fingerprint == report_fingerprint
        )

        try:
            candidate_archives = len(tuple(candidates_root.glob("g*.tar.zst")))
        except OSError:
            candidate_archives = 0
        try:
            launcher_archives = len(tuple(launchers_root.glob("g*.tar.zst")))
        except OSError:
            launcher_archives = 0
        final_archive = (launchers_root / "run-final.tar.zst").is_file()

        if exact_cache:
            assert cached is not None
            old = cached.telemetry
            if (
                old.archived_candidate_generations == candidate_archives
                and old.archived_launcher_generations == launcher_archives
                and old.final_launcher_archive == final_archive
            ):
                return old
            telemetry = _LaunchTelemetry(
                manifest_generations=old.manifest_generations,
                fireworks_total=old.fireworks_total,
                fireworks_completed=old.fireworks_completed,
                fireworks_fizzled=old.fireworks_fizzled,
                fireworks_archived=old.fireworks_archived,
                launches_total=old.launches_total,
                inspection_errors=old.inspection_errors,
                archived_candidate_generations=candidate_archives,
                archived_launcher_generations=launcher_archives,
                final_launcher_archive=final_archive,
            )
            cached.telemetry = telemetry
            return telemetry

        can_extend = (
            cached is not None
            and len(manifest_fingerprint) >= len(cached.manifest_fingerprint)
            and manifest_fingerprint[: len(cached.manifest_fingerprint)]
            == cached.manifest_fingerprint
            and len(report_fingerprint) >= len(cached.report_fingerprint)
            and report_fingerprint[: len(cached.report_fingerprint)]
            == cached.report_fingerprint
        )

        if can_extend:
            assert cached is not None
            seen_fw_ids = set(cached.seen_fw_ids)
            previous = cached.telemetry
            manifest_generations = previous.manifest_generations
            fireworks_total = previous.fireworks_total
            fireworks_completed = previous.fireworks_completed
            fireworks_fizzled = previous.fireworks_fizzled
            fireworks_archived = previous.fireworks_archived
            launches_total = previous.launches_total
            inspection_errors = previous.inspection_errors
            new_manifest_paths = manifest_paths[len(cached.manifest_fingerprint):]
            new_report_paths = report_paths[len(cached.report_fingerprint):]
        else:
            seen_fw_ids: set[str] = set()
            manifest_generations = 0
            fireworks_total = 0
            fireworks_completed = 0
            fireworks_fizzled = 0
            fireworks_archived = 0
            launches_total = 0
            inspection_errors = 0
            new_manifest_paths = manifest_paths
            new_report_paths = report_paths

        for manifest_path in new_manifest_paths:
            payload = self._read_json_object(manifest_path)
            manifest_generations += 1
            for fw in payload.get("fireworks") or []:
                if not isinstance(fw, dict):
                    continue
                raw_fw_id = fw.get("fw_id")
                fw_key = (
                    str(raw_fw_id)
                    if raw_fw_id is not None
                    else f"{manifest_path}:{fireworks_total}"
                )
                if fw_key in seen_fw_ids:
                    continue
                seen_fw_ids.add(fw_key)
                fireworks_total += 1
                state = str(fw.get("state") or "").upper()
                fireworks_completed += int(state == "COMPLETED")
                fireworks_fizzled += int(state == "FIZZLED")
                fireworks_archived += int(state == "ARCHIVED")
                if fw.get("inspection_error"):
                    inspection_errors += 1
                launches = fw.get("launches") or []
                if isinstance(launches, list):
                    launches_total += len(launches)
                    inspection_errors += sum(
                        1
                        for launch in launches
                        if isinstance(launch, dict)
                        and launch.get("inspection_error")
                    )

        for report_path in new_report_paths:
            report = self._read_json_object(report_path)
            skipped = report.get("skipped_launchers") or []
            if isinstance(skipped, list):
                inspection_errors += len(skipped)

        telemetry = _LaunchTelemetry(
            manifest_generations=manifest_generations,
            fireworks_total=fireworks_total,
            fireworks_completed=fireworks_completed,
            fireworks_fizzled=fireworks_fizzled,
            fireworks_archived=fireworks_archived,
            launches_total=launches_total,
            inspection_errors=inspection_errors,
            archived_candidate_generations=candidate_archives,
            archived_launcher_generations=launcher_archives,
            final_launcher_archive=final_archive,
        )
        cache = _CachedLaunchTelemetry(
            directory_fingerprint=directory_fingerprint,
            manifest_fingerprint=manifest_fingerprint,
            report_fingerprint=report_fingerprint,
            seen_fw_ids=seen_fw_ids,
            telemetry=telemetry,
        )
        with self._launch_cache_lock:
            self._launch_cache[cache_key] = cache
        return telemetry

    @staticmethod
    def _objective_from_generation_best(row: dict[str, Any]) -> float | None:
        best = row.get("generation_best")
        if not isinstance(best, dict):
            return None
        raw = best.get("objective")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def _snapshot_and_history_from_summary(
        self,
        run: RunReference,
    ) -> tuple[RunSnapshot, tuple[EvaluationPoint, ...]]:
        rows = self._summary_rows(run.run_root)
        metadata = self._metadata_for(run.run_id, run.run_root)
        telemetry = self._launch_telemetry(run.run_root)
        history: list[EvaluationPoint] = []
        cumulative = 0
        best_so_far: float | None = None
        latest_objective: float | None = None
        best_candidate_id: str | None = None

        ordered_rows = sorted(
            rows,
            key=lambda row: self._nonnegative_int(row.get("generation_id"))
            if self._nonnegative_int(row.get("generation_id")) is not None
            else 10**12,
        )
        for row in ordered_rows:
            actual = self._nonnegative_int(row.get("actual_count")) or 0
            cumulative += actual
            generation_id = self._nonnegative_int(row.get("generation_id"))
            generation_best = row.get("generation_best")
            objective = self._objective_from_generation_best(row)
            candidate_id = (
                str(generation_best.get("candidate_id"))
                if isinstance(generation_best, dict)
                and generation_best.get("candidate_id") is not None
                else None
            )
            if objective is not None:
                latest_objective = objective
                if best_so_far is None:
                    best_so_far = objective
                    best_candidate_id = candidate_id
                elif run.direction is ObjectiveDirection.MAXIMIZE:
                    if objective > best_so_far:
                        best_so_far = objective
                        best_candidate_id = candidate_id
                elif objective < best_so_far:
                    best_so_far = objective
                    best_candidate_id = candidate_id
            if cumulative > 0:
                history.append(
                    EvaluationPoint(
                        evaluation=cumulative,
                        candidate_id=candidate_id,
                        status="ok" if objective is not None else "unknown",
                        objective=objective,
                        best_so_far=best_so_far,
                        mean_so_far=None,
                        median_so_far=None,
                        generation_id=generation_id,
                    )
                )

        best = metadata.get("best")
        if isinstance(best, dict):
            try:
                metadata_best = float(best.get("objective"))
            except (TypeError, ValueError):
                metadata_best = None
            if metadata_best is not None and math.isfinite(metadata_best):
                best_so_far = metadata_best
                raw_candidate = best.get("candidate_id")
                best_candidate_id = (
                    str(raw_candidate) if raw_candidate is not None else None
                )

        evaluations_done = self._nonnegative_int(metadata.get("evaluations_done"))
        evaluation_count = evaluations_done if evaluations_done is not None else cumulative
        finalized = bool(metadata.get("finalized", False))
        state = (
            RunState.COMPLETED
            if finalized
            else self._state_from_artifacts(run, has_records=bool(evaluation_count))
        )
        finished_at = None
        if finalized:
            finished_at = self._timestamp_value(metadata.get("updated_at"))
            if finished_at is None and ordered_rows:
                finished_at = self._timestamp_value(ordered_rows[-1].get("timestamp"))

        snapshot = RunSnapshot(
            reference=run,
            state=state,
            evaluation_count=evaluation_count,
            failed_evaluations=0,
            result_sources=len(rows),
            successful_evaluations=0,
            best_objective=best_so_far,
            latest_objective=latest_objective,
            mean_objective=None,
            median_objective=None,
            best_candidate_id=best_candidate_id,
            planned_evaluations=self._nonnegative_int(metadata.get("max_evaluations")),
            completed_generations=self._nonnegative_int(metadata.get("completed_generations")),
            history_is_sampled=True,
            population_diversity=(),
            run_started_at=None,
            run_finished_at=finished_at,
            artifact_mode="generation-summary",
            quality_metrics_complete=False,
            fireworks_total=telemetry.fireworks_total,
            fireworks_completed=telemetry.fireworks_completed,
            fireworks_fizzled=telemetry.fireworks_fizzled,
            fireworks_archived=telemetry.fireworks_archived,
            launches_total=telemetry.launches_total,
            launch_manifest_generations=telemetry.manifest_generations,
            archived_candidate_generations=telemetry.archived_candidate_generations,
            archived_launcher_generations=telemetry.archived_launcher_generations,
            final_launcher_archive=telemetry.final_launcher_archive,
            launcher_inspection_errors=telemetry.inspection_errors,
        )
        return snapshot, tuple(history)

    def _empty_htc_snapshot(
        self,
        run: RunReference,
    ) -> tuple[RunSnapshot, tuple[EvaluationPoint, ...]]:
        metadata = self._metadata_for(run.run_id, run.run_root)
        telemetry = self._launch_telemetry(run.run_root)
        snapshot = RunSnapshot(
            reference=run,
            state=self._state_from_artifacts(run, has_records=False),
            evaluation_count=self._nonnegative_int(metadata.get("evaluations_done")) or 0,
            failed_evaluations=0,
            result_sources=0,
            successful_evaluations=0,
            best_objective=(
                self._fitness_objective({"fitness": metadata.get("best")})
                if isinstance(metadata.get("best"), dict)
                else None
            ),
            best_candidate_id=(
                str(metadata["best"].get("candidate_id"))
                if isinstance(metadata.get("best"), dict)
                and metadata["best"].get("candidate_id") is not None
                else None
            ),
            planned_evaluations=self._nonnegative_int(metadata.get("max_evaluations")),
            completed_generations=self._nonnegative_int(metadata.get("completed_generations")),
            artifact_mode="htc-control",
            quality_metrics_complete=False,
            fireworks_total=telemetry.fireworks_total,
            fireworks_completed=telemetry.fireworks_completed,
            fireworks_fizzled=telemetry.fireworks_fizzled,
            fireworks_archived=telemetry.fireworks_archived,
            launches_total=telemetry.launches_total,
            launch_manifest_generations=telemetry.manifest_generations,
            archived_candidate_generations=telemetry.archived_candidate_generations,
            archived_launcher_generations=telemetry.archived_launcher_generations,
            final_launcher_archive=telemetry.final_launcher_archive,
            launcher_inspection_errors=telemetry.inspection_errors,
        )
        return snapshot, ()

    def snapshot_and_history_of(
        self,
        run: RunReference,
    ) -> tuple[RunSnapshot, tuple[EvaluationPoint, ...]]:
        if self._should_use_summary_mode(run):
            return self._snapshot_and_history_from_summary(run)

        shard_paths = self._generation_shard_paths(run.run_root)
        if shard_paths:
            cached = self._snapshot_and_history_from_shards(
                run,
                shard_paths,
            )
            return cached.snapshot, cached.history

        # New HTC runs intentionally avoid exposing millions of candidate
        # workdirs as the live monitoring API. Before the first generation
        # shard exists, observe only lightweight control/launcher artifacts.
        if self.data_mode != "full" and self._is_htc_artifact_run(run.run_root):
            return self._empty_htc_snapshot(run)

        records = self._records_by_attempt(run.run_root)
        return (
            self._snapshot_from_records(run, records),
            self._history_from_records(run, records),
        )

    def _snapshot_from_records(
        self,
        run: RunReference,
        records: dict[str, dict[str, Any]],
    ) -> RunSnapshot:
        failed = sum(
            1
            for record in records.values()
            if self._fitness_status(record) != "ok"
        )

        valid_results: list[tuple[str | None, float]] = []
        for record in records.values():
            if self._fitness_status(record) != "ok":
                continue
            objective = self._fitness_objective(record)
            if objective is None:
                continue
            valid_results.append((self._candidate_id(record), objective))

        objectives = [objective for _candidate_id, objective in valid_results]
        best_candidate_id: str | None = None
        best_objective: float | None = None
        latest_objective: float | None = None
        mean_objective: float | None = None
        median_objective: float | None = None

        if valid_results:
            best_selector = (
                max
                if run.direction is ObjectiveDirection.MAXIMIZE
                else min
            )
            best_candidate_id, best_objective = best_selector(
                valid_results,
                key=lambda item: item[1],
            )
            latest_objective = valid_results[-1][1]
            mean_objective = statistics.fmean(objectives)
            median_objective = statistics.median(objectives)

        source_paths = {
            str(record.get("_monitor_source", ""))
            for record in records.values()
            if record.get("_monitor_source")
        }
        started_values = tuple(
            timestamp
            for record in records.values()
            if (
                timestamp := self._timestamp_value(
                    record.get("started_at")
                )
            )
            is not None
        )
        finished_values = tuple(
            timestamp
            for record in records.values()
            if (
                timestamp := self._timestamp_value(
                    record.get("finished_at")
                )
            )
            is not None
        )
        metadata = self._metadata_for(run.run_id, run.run_root)
        telemetry = self._launch_telemetry(run.run_root)
        return RunSnapshot(
            reference=run,
            state=self._state_from_records(run, records),
            evaluation_count=len(records),
            failed_evaluations=failed,
            result_sources=len(source_paths),
            successful_evaluations=len(valid_results),
            best_objective=best_objective,
            latest_objective=latest_objective,
            mean_objective=mean_objective,
            median_objective=median_objective,
            best_candidate_id=best_candidate_id,
            run_started_at=(
                min(started_values) if started_values else None
            ),
            run_finished_at=(
                max(finished_values) if finished_values else None
            ),
            planned_evaluations=self._nonnegative_int(
                metadata.get("max_evaluations")
            ),
            completed_generations=self._nonnegative_int(
                metadata.get("completed_generations")
            ),
            artifact_mode="legacy-records",
            quality_metrics_complete=True,
            fireworks_total=telemetry.fireworks_total,
            fireworks_completed=telemetry.fireworks_completed,
            fireworks_fizzled=telemetry.fireworks_fizzled,
            fireworks_archived=telemetry.fireworks_archived,
            launches_total=telemetry.launches_total,
            launch_manifest_generations=telemetry.manifest_generations,
            archived_candidate_generations=telemetry.archived_candidate_generations,
            archived_launcher_generations=telemetry.archived_launcher_generations,
            final_launcher_archive=telemetry.final_launcher_archive,
            launcher_inspection_errors=telemetry.inspection_errors,
        )

    def _history_from_records(
        self,
        run: RunReference,
        records: dict[str, dict[str, Any]],
    ) -> tuple[EvaluationPoint, ...]:
        history: list[EvaluationPoint] = []
        running_median = _RunningMedian()
        objective_sum = 0.0
        valid_count = 0
        best_so_far: float | None = None

        for evaluation, record in enumerate(records.values(), start=1):
            status = self._fitness_status(record)
            objective = (
                self._fitness_objective(record)
                if status == "ok"
                else None
            )

            if objective is not None:
                objective_sum += objective
                valid_count += 1
                running_median.add(objective)
                if best_so_far is None:
                    best_so_far = objective
                elif run.direction is ObjectiveDirection.MAXIMIZE:
                    best_so_far = max(best_so_far, objective)
                else:
                    best_so_far = min(best_so_far, objective)

            mean_so_far = (
                objective_sum / valid_count
                if valid_count
                else None
            )
            history.append(
                EvaluationPoint(
                    evaluation=evaluation,
                    candidate_id=self._candidate_id(record),
                    status=status,
                    objective=objective,
                    best_so_far=best_so_far,
                    mean_so_far=mean_so_far,
                    median_so_far=running_median.value,
                    generation_id=self._generation_id(record),
                    parameters=self._parameter_items(record),
                )
            )

        return tuple(history)

    @staticmethod
    def _generation_shard_paths(
        run_root: Path,
    ) -> tuple[Path, ...]:
        generation_root = run_root / "generations"
        if not generation_root.is_dir():
            return ()
        try:
            return tuple(sorted(generation_root.glob("g*.jsonl")))
        except OSError:
            return ()

    def _snapshot_and_history_from_shards(
        self,
        run: RunReference,
        shard_paths: tuple[Path, ...],
    ) -> _CachedShardRun:
        metadata_path = run.run_root / "summary.json"
        final_results_path = run.run_root / "results.jsonl"
        summary_jsonl_path = run.run_root / "summary.jsonl"

        shard_fingerprint_items: list[tuple[str, int, int]] = []
        readable_shards: list[Path] = []
        for path in shard_paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            readable_shards.append(path)
            shard_fingerprint_items.append(
                (str(path), stat.st_size, stat.st_mtime_ns)
            )
        shard_fingerprint = tuple(shard_fingerprint_items)

        control_root = run.run_root / "control"
        checkpoint_root = run.run_root / "checkpoint"
        state_paths: list[Path] = [
            metadata_path,
            summary_jsonl_path,
            final_results_path,
            control_root / "pause.request.json",
            control_root / "pause.ack.json",
            checkpoint_root / "manifest.json",
        ]
        generation_root = run.run_root / "generations"
        archive_root = run.run_root / "archives"
        state_paths.extend(
            [
                generation_root,
                archive_root / "manifests",
                archive_root / "candidates",
                archive_root / "launchers",
                archive_root / "launchers" / "run-final.tar.zst",
            ]
        )

        state_fingerprint_items: list[tuple[str, int, int]] = []
        for path in state_paths:
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            state_fingerprint_items.append(
                (str(path), stat.st_size, stat.st_mtime_ns)
            )
        state_fingerprint = tuple(state_fingerprint_items)
        cache_key = run.run_root.resolve()

        with self._shard_cache_lock:
            cached = self._shard_cache.get(cache_key)

        if cached is not None and cached.shard_fingerprint == shard_fingerprint:
            if cached.state_fingerprint == state_fingerprint:
                return cached
            accumulator = cached.accumulator
            source_count = cached.source_count
        elif (
            cached is not None
            and len(shard_fingerprint) >= len(cached.shard_fingerprint)
            and shard_fingerprint[: len(cached.shard_fingerprint)]
            == cached.shard_fingerprint
        ):
            # GOW generation shards are immutable after finalization. If the
            # old fingerprint is an exact prefix, process only newly published
            # generations instead of rescanning the entire campaign.
            accumulator = cached.accumulator
            source_count = cached.source_count
            start_index = len(cached.shard_fingerprint)
            for shard_path in readable_shards[start_index:]:
                source_count += 1
                for line_number, payload in self._read_jsonl(shard_path):
                    source = Path(f"{shard_path}#{line_number}")
                    accumulator.consume(payload, source=source)
        else:
            accumulator = _ShardRunAccumulator(run)
            source_count = 0
            for shard_path in readable_shards:
                source_count += 1
                for line_number, payload in self._read_jsonl(shard_path):
                    source = Path(f"{shard_path}#{line_number}")
                    accumulator.consume(payload, source=source)

        metadata = self._metadata_for(run.run_id, run.run_root)
        finalized = bool(metadata.get("finalized", False))
        state = (
            RunState.COMPLETED
            if finalized
            else self._state_from_artifacts(
                run,
                has_records=bool(accumulator.evaluation_count),
            )
        )
        mean_objective = (
            accumulator.objective_sum / accumulator.successful_evaluations
            if accumulator.successful_evaluations
            else None
        )
        history = accumulator.history()
        telemetry = self._launch_telemetry(run.run_root)
        snapshot = RunSnapshot(
            reference=run,
            state=state,
            evaluation_count=accumulator.evaluation_count,
            failed_evaluations=accumulator.failed_evaluations,
            result_sources=source_count,
            successful_evaluations=accumulator.successful_evaluations,
            best_objective=accumulator.best_objective,
            latest_objective=accumulator.latest_objective,
            mean_objective=mean_objective,
            median_objective=accumulator.median.value,
            best_candidate_id=accumulator.best_candidate_id,
            run_started_at=accumulator.run_started_at,
            run_finished_at=accumulator.run_finished_at,
            planned_evaluations=self._nonnegative_int(
                metadata.get("max_evaluations")
            ),
            completed_generations=self._nonnegative_int(
                metadata.get("completed_generations")
            ),
            history_is_sampled=(
                accumulator.evaluation_count > len(history)
            ),
            population_diversity=accumulator.diversity(),
            artifact_mode="generation-shards",
            quality_metrics_complete=True,
            fireworks_total=telemetry.fireworks_total,
            fireworks_completed=telemetry.fireworks_completed,
            fireworks_fizzled=telemetry.fireworks_fizzled,
            fireworks_archived=telemetry.fireworks_archived,
            launches_total=telemetry.launches_total,
            launch_manifest_generations=telemetry.manifest_generations,
            archived_candidate_generations=telemetry.archived_candidate_generations,
            archived_launcher_generations=telemetry.archived_launcher_generations,
            final_launcher_archive=telemetry.final_launcher_archive,
            launcher_inspection_errors=telemetry.inspection_errors,
        )
        result = _CachedShardRun(
            shard_fingerprint=shard_fingerprint,
            state_fingerprint=state_fingerprint,
            accumulator=accumulator,
            source_count=source_count,
            snapshot=snapshot,
            history=history,
        )
        with self._shard_cache_lock:
            self._shard_cache[cache_key] = result
        return result

    @staticmethod
    def _timestamp_value(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None

        if isinstance(value, (int, float)):
            numeric = float(value)
            if math.isfinite(numeric) and numeric >= 0.0:
                return numeric
            return None

        text = str(value).strip()
        if not text:
            return None

        try:
            numeric = float(text)
        except ValueError:
            numeric = None
        if (
            numeric is not None
            and math.isfinite(numeric)
            and numeric >= 0.0
        ):
            return numeric

        normalized = (
            text[:-1] + "+00:00"
            if text.upper().endswith("Z")
            else text
        )
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        timestamp = parsed.timestamp()
        if not math.isfinite(timestamp) or timestamp < 0.0:
            return None
        return timestamp

    @staticmethod
    def _nonnegative_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    def diagnostics(self) -> dict[str, object]:
        references = self.discover_runs()
        return {
            "selected_path": str(self.selected_path),
            "results_root": str(self.results_root),
            "runs_root": str(self.runs_root),
            "direct_run_root": (
                str(self.direct_run_root)
                if self.direct_run_root is not None
                else None
            ),
            "runs_found": len(references),
            "run_ids": [reference.run_id for reference in references],
            "data_mode": self.data_mode,
            "summary_threshold": self.summary_threshold,
        }

    def _build_reference(self, run_root: Path) -> RunReference:
        metadata = self._metadata_for(run_root.name, run_root)
        direction = self._parse_direction(metadata)
        problem_id = metadata.get("problem_id")
        if not isinstance(problem_id, str) or not problem_id.strip():
            problem_id = self._problem_id_from_first_result(run_root)

        return RunReference(
            run_id=run_root.name,
            run_root=run_root.resolve(),
            direction=direction,
            problem_id=problem_id,
        )

    def _metadata_for(
        self,
        run_id: str,
        run_root: Path,
    ) -> dict[str, Any]:
        for path in (
            run_root / "summary.json",
            self.results_root / "summary.json",
        ):
            payload = self._read_json_object(path)
            if not payload:
                continue
            payload_run_id = payload.get("run_id")
            if payload_run_id is None or str(payload_run_id) == run_id:
                return payload
        return {}

    @staticmethod
    def _parse_direction(
        metadata: dict[str, Any],
    ) -> ObjectiveDirection:
        objective = metadata.get("objective")
        raw_direction = (
            objective.get("direction")
            if isinstance(objective, dict)
            else metadata.get("direction")
        )
        try:
            return ObjectiveDirection(str(raw_direction).lower())
        except ValueError:
            return ObjectiveDirection.UNKNOWN

    def _problem_id_from_first_result(
        self,
        run_root: Path,
    ) -> str | None:
        shard_paths = self._generation_shard_paths(run_root)
        if shard_paths:
            for _line_number, record in self._read_jsonl(
                shard_paths[0]
            ):
                problem_id = record.get("problem_id")
                if isinstance(problem_id, str) and problem_id.strip():
                    return problem_id
                break

        for record in self._records_by_attempt(run_root).values():
            problem_id = record.get("problem_id")
            if isinstance(problem_id, str) and problem_id.strip():
                return problem_id
        return None

    def _records_by_attempt(
        self,
        run_root: Path,
    ) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}

        try:
            result_paths = sorted(run_root.rglob("result.json"))
        except OSError:
            result_paths = []

        for path in result_paths:
            payload = self._read_json_object(path)
            if payload:
                payload["_monitor_source"] = str(path)
                records[self._record_key(payload, path)] = payload

        final_results = run_root / "results.jsonl"
        for line_number, payload in self._read_jsonl(final_results):
            payload["_monitor_source"] = str(final_results)
            key_path = Path(f"{final_results}#{line_number}")
            records[self._record_key(payload, key_path)] = payload

        return records

    @staticmethod
    def _record_key(payload: dict[str, Any], source: Path) -> str:
        for key in ("attempt_id", "candidate_id"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                return f"{key}:{value}"
        return f"source:{source}"

    @staticmethod
    def _candidate_id(payload: dict[str, Any]) -> str | None:
        value = payload.get("candidate_id")
        if value is None or not str(value).strip():
            return None
        return str(value)

    @staticmethod
    def _generation_id(payload: dict[str, Any]) -> int | None:
        raw_generation = payload.get("generation_id")
        if not isinstance(raw_generation, bool):
            try:
                generation = int(raw_generation)
            except (TypeError, ValueError):
                generation = -1
            if generation >= 0:
                return generation

        for key in ("candidate_local_id", "candidate_id"):
            value = payload.get(key)
            if value is None:
                continue
            match = re.search(r"(?:^|_)g(\d+)(?:_|$)", str(value))
            if match is not None:
                return int(match.group(1))
        return None

    @staticmethod
    def _parameter_items(
        payload: dict[str, Any],
    ) -> tuple[tuple[str, float], ...]:
        raw_parameters = payload.get("params")
        if not isinstance(raw_parameters, dict):
            return ()

        parameters: list[tuple[str, float]] = []
        for raw_name, raw_value in raw_parameters.items():
            if isinstance(raw_value, bool):
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            parameters.append((str(raw_name), value))

        return tuple(sorted(parameters))

    @staticmethod
    def _fitness_status(payload: dict[str, Any]) -> str:
        fitness = payload.get("fitness")
        if isinstance(fitness, dict):
            return str(fitness.get("status", "unknown")).lower()
        return str(payload.get("status", "unknown")).lower()

    @staticmethod
    def _fitness_objective(payload: dict[str, Any]) -> float | None:
        fitness = payload.get("fitness")
        raw_objective = (
            fitness.get("objective")
            if isinstance(fitness, dict)
            else payload.get("objective")
        )
        if isinstance(raw_objective, bool):
            return None
        try:
            objective = float(raw_objective)
        except (TypeError, ValueError):
            return None
        return objective if math.isfinite(objective) else None

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _read_jsonl(
        path: Path,
    ) -> Iterator[tuple[int, dict[str, Any]]]:
        if not path.is_file():
            return
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                for line_number, line in enumerate(handle, start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        payload = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        yield line_number, payload
        except (OSError, UnicodeError):
            return
