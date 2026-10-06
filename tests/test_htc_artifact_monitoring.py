from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gow_monitor.domain import RunState
from gow_monitor.infrastructure.gow_results import GowFilesystemRunReader


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_summary_run(run_root: Path) -> None:
    _write_json(
        run_root / "summary.json",
        {
            "problem_id": "mica-large",
            "run_id": run_root.name,
            "max_evaluations": 10_000_000,
            "evaluations_done": 20_000,
            "completed_generations": 2,
            "finalized": False,
            "objective": {"direction": "minimize"},
            "best": {
                "candidate_id": "best-g1",
                "generation_id": 1,
                "objective": 0.5,
            },
        },
    )
    rows = [
        {
            "timestamp": "2026-10-06T10:00:00Z",
            "run_id": run_root.name,
            "problem_id": "mica-large",
            "generation_id": 0,
            "expected_count": 10_000,
            "actual_count": 10_000,
            "generation_best": {
                "candidate_id": "best-g0",
                "generation_id": 0,
                "objective": 1.0,
            },
        },
        {
            "timestamp": "2026-10-06T10:01:00Z",
            "run_id": run_root.name,
            "problem_id": "mica-large",
            "generation_id": 1,
            "expected_count": 10_000,
            "actual_count": 10_000,
            "generation_best": {
                "candidate_id": "best-g1",
                "generation_id": 1,
                "objective": 0.5,
            },
        },
    ]
    (run_root / "summary.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_auto_mode_uses_generation_summary_for_large_htc_run(tmp_path: Path) -> None:
    results_root = tmp_path / "results"
    run_root = results_root / "runs" / "run-large"
    generation_root = run_root / "generations"
    generation_root.mkdir(parents=True)
    _write_summary_run(run_root)

    # Invalid shard contents prove scalable summary mode does not need to read
    # multi-million-candidate generation files for the live dashboard.
    (generation_root / "g000000.jsonl").write_text("not-json\n", encoding="utf-8")
    (generation_root / "g000001.jsonl").write_text("not-json\n", encoding="utf-8")

    reader = GowFilesystemRunReader(results_root, data_mode="auto")
    reference = reader.discover_runs()[0]
    snapshot, history = reader.snapshot_and_history_of(reference)

    assert snapshot.artifact_mode == "generation-summary"
    assert snapshot.quality_metrics_complete is False
    assert snapshot.evaluation_count == 20_000
    assert snapshot.planned_evaluations == 10_000_000
    assert snapshot.completed_generations == 2
    assert snapshot.best_objective == 0.5
    assert snapshot.best_candidate_id == "best-g1"
    assert snapshot.state is RunState.RUNNING
    assert [point.evaluation for point in history] == [10_000, 20_000]
    assert history[-1].best_so_far == 0.5


def test_launch_manifests_and_cold_archives_are_observed_without_unpacking(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    run_root = results_root / "runs" / "run-launches"
    _write_summary_run(run_root)

    generation_root = run_root / "generations"
    _write_json(
        generation_root / "g000000.launches.json",
        {
            "generation_id": 0,
            "all_terminal": True,
            "fw_ids": [10, 11, 12],
            "fireworks": [
                {
                    "fw_id": 10,
                    "state": "COMPLETED",
                    "terminal": True,
                    "launches": [{"launch_id": 1, "state": "COMPLETED"}],
                },
                {
                    "fw_id": 11,
                    "state": "FIZZLED",
                    "terminal": True,
                    "launches": [{"launch_id": 2, "state": "FIZZLED"}],
                },
                {
                    "fw_id": 12,
                    "state": "ARCHIVED",
                    "terminal": True,
                    "launches": [{"launch_id": 3, "state": "ARCHIVED"}],
                },
            ],
        },
    )
    candidates_archive = run_root / "archives" / "candidates" / "g000000.tar.zst"
    launchers_archive = run_root / "archives" / "launchers" / "g000000.tar.zst"
    final_archive = run_root / "archives" / "launchers" / "run-final.tar.zst"
    for path in (candidates_archive, launchers_archive, final_archive):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"archive-marker-only")
    _write_json(
        run_root / "archives" / "manifests" / "g000000.archive.json",
        {"generation_id": 0, "skipped_launchers": []},
    )

    reader = GowFilesystemRunReader(results_root, data_mode="summary")
    snapshot = reader.snapshot_of(reader.discover_runs()[0])

    assert snapshot.fireworks_total == 3
    assert snapshot.fireworks_completed == 1
    assert snapshot.fireworks_fizzled == 1
    assert snapshot.fireworks_archived == 1
    assert snapshot.launches_total == 3
    assert snapshot.launch_manifest_generations == 1
    assert snapshot.archived_candidate_generations == 1
    assert snapshot.archived_launcher_generations == 1
    assert snapshot.final_launcher_archive is True


def test_full_mode_processes_only_new_immutable_generation_shards(
    monkeypatch,
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    run_root = results_root / "runs" / "run-incremental"
    generation_root = run_root / "generations"
    generation_root.mkdir(parents=True)
    _write_json(
        run_root / "summary.json",
        {
            "run_id": "run-incremental",
            "problem_id": "toy",
            "max_evaluations": 4,
            "completed_generations": 1,
            "objective": {"direction": "minimize"},
        },
    )

    shard0 = generation_root / "g000000.jsonl"
    shard0.write_text("{}\n", encoding="utf-8")
    shard1 = generation_root / "g000001.jsonl"

    calls: list[str] = []

    def fake_read_jsonl(path: Path):
        calls.append(path.name)
        generation = int(path.stem[1:])
        for offset in range(2):
            index = generation * 2 + offset
            candidate_id = f"c{index}"
            yield offset + 1, {
                "candidate_id": candidate_id,
                "attempt_id": f"{candidate_id}_a000",
                "generation_id": generation,
                "fitness": {"status": "ok", "objective": float(10 - index)},
            }

    monkeypatch.setattr(
        GowFilesystemRunReader,
        "_read_jsonl",
        staticmethod(fake_read_jsonl),
    )
    GowFilesystemRunReader._shard_cache.clear()

    reader = GowFilesystemRunReader(results_root, data_mode="full")
    reference = reader.discover_runs()[0]
    first = reader.snapshot_of(reference)
    assert first.evaluation_count == 2
    assert calls == ["g000000.jsonl"]

    shard1.write_text("{}\n", encoding="utf-8")
    second = reader.snapshot_of(reference)
    assert second.evaluation_count == 4
    assert calls == ["g000000.jsonl", "g000001.jsonl"]

    third = reader.snapshot_of(reference)
    assert third.evaluation_count == 4
    assert calls == ["g000000.jsonl", "g000001.jsonl"]


def test_auto_mode_does_not_recursively_scan_candidate_workdirs_before_first_shard(
    tmp_path: Path,
) -> None:
    results_root = tmp_path / "results"
    run_root = results_root / "runs" / "run-starting"
    (run_root / "launchers").mkdir(parents=True)
    candidate = run_root / "candidate-1"
    candidate.mkdir()
    _write_json(
        candidate / "result.json",
        {
            "candidate_id": "candidate-1",
            "fitness": {"status": "ok", "objective": 1.0},
        },
    )

    auto_reader = GowFilesystemRunReader(results_root, data_mode="auto")
    auto_ref = auto_reader.discover_runs()[0]
    auto_snapshot = auto_reader.snapshot_of(auto_ref)
    assert auto_snapshot.evaluation_count == 0
    assert auto_snapshot.artifact_mode == "htc-control"

    full_reader = GowFilesystemRunReader(results_root, data_mode="full")
    full_ref = full_reader.discover_runs()[0]
    full_snapshot = full_reader.snapshot_of(full_ref)
    assert full_snapshot.evaluation_count == 1
    assert full_snapshot.artifact_mode == "legacy-records"


def test_run_root_can_be_discovered_after_candidate_directories_are_archived(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "detached-run"
    run_root.mkdir()
    (run_root / "summary.jsonl").write_text("\n", encoding="utf-8")

    resolution = GowFilesystemRunReader.resolve_selected_path(run_root)
    assert resolution.direct_run_root == run_root.resolve()
