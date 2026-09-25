#!/usr/bin/env python3
"""Remove the overlapping legacy T3 metric and rebuild existing Jaeger analyses."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path

from analyze_results import generate_jaeger_analysis
from jaeger_collector import write_quality_artifacts
from run_factorial import combine, write_campaign_report


REMOVED_METRIC = "nginx_proxy_worker_allocation_latency"
DERIVED_FILES = (
    "derived_observations.csv",
    "derived_samples.csv",
    "derived_repetition_summary.csv",
    "data_quality.csv",
    "DATA_QUALITY.md",
    "collection_metadata.json",
)
DEFAULT_THRESHOLDS = {
    "warn_invalid_percent": 5.0,
    "fail_invalid_percent": 20.0,
    "warn_t4_coverage_percent": 90.0,
    "fail_t4_coverage_percent": 70.0,
    "warn_zero_percent": 80.0,
}


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def filter_metric_csv(path: Path) -> None:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = [row for row in reader if row.get("operation", row.get("metric")) != REMOVED_METRIC]
    write_csv(path, rows, fields)


def booleanize_observations(rows: list[dict]) -> list[dict]:
    converted = []
    for row in rows:
        item = dict(row)
        item["valid"] = str(item.get("valid", "")).lower() == "true"
        converted.append(item)
    return converted


def reanalyze_campaign(root: Path) -> None:
    cells = sorted(path for path in root.glob("users-*__rep-*") if path.is_dir())
    if not cells:
        raise RuntimeError(f"no completed cells found in {root}")

    root_backup = root / "pre-t2-t3-correction"
    root_backup.mkdir(exist_ok=True)
    for name in (
        "all_jaeger_derived_repetition_summaries.csv",
        "all_jaeger_derived_samples.csv",
        "EXPERIMENT_REPORT.md",
    ):
        source = root / name
        destination = root_backup / name
        if source.is_file() and not destination.exists():
            shutil.copy2(source, destination)

    analysis = root / "analysis" / "jaeger-derived"
    analysis_backup = root / "analysis" / "jaeger-derived-pre-t2-t3-correction"
    if analysis.exists() and not analysis_backup.exists():
        shutil.move(analysis, analysis_backup)
    elif analysis.exists():
        index = 2
        repeated_backup = root / "analysis" / f"jaeger-derived-pre-reanalysis-{index}"
        while repeated_backup.exists():
            index += 1
            repeated_backup = root / "analysis" / f"jaeger-derived-pre-reanalysis-{index}"
        shutil.move(analysis, repeated_backup)

    summary_paths = []
    sample_paths = []
    quality_paths = []
    for cell in cells:
        jaeger = cell / "jaeger"
        backup = jaeger / "pre-t2-t3-correction"
        backup.mkdir(exist_ok=True)
        for name in DERIVED_FILES:
            source = jaeger / name
            destination = backup / name
            if source.is_file() and not destination.exists():
                shutil.copy2(source, destination)

        for name in ("derived_observations.csv", "derived_samples.csv",
                     "derived_repetition_summary.csv"):
            filter_metric_csv(jaeger / name)

        metadata_path = jaeger / "collection_metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["derived_metrics"] = [
            metric for metric in metadata.get("derived_metrics", [])
            if metric != REMOVED_METRIC
        ]
        metadata.get("derived_metric_formulas", {}).pop(REMOVED_METRIC, None)
        metadata.get("derived_observation_counts", {}).pop(REMOVED_METRIC, None)
        metadata["t2_t3_definition"] = {
            "T2": "nginx_proxy_client_overhead",
            "T3": "nginx_worker_connection_latency",
            "overlap": False,
            "note": "T2 is residual; T3 observes only upstream connection establishment.",
        }
        observations = booleanize_observations(read_csv(jaeger / "derived_observations.csv"))
        summaries = read_csv(jaeger / "derived_repetition_summary.csv")
        thresholds = metadata.get("data_quality", {}).get(
            "thresholds", DEFAULT_THRESHOLDS
        )
        report = write_quality_artifacts(
            jaeger, observations, summaries, metadata, thresholds,
        )
        metadata["data_quality"] = {
            "overall": report["overall"], "thresholds": thresholds,
        }
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

        cell_metadata_path = cell / "metadata.json"
        if cell_metadata_path.is_file():
            cell_backup = backup / "cell_metadata.json"
            if not cell_backup.exists():
                shutil.copy2(cell_metadata_path, cell_backup)
            cell_metadata = json.loads(cell_metadata_path.read_text(encoding="utf-8"))
            cell_metadata["jaeger_metrics"] = [
                metric for metric in cell_metadata.get("jaeger_metrics", [])
                if metric != REMOVED_METRIC
            ]
            cell_metadata["jaeger_collection"] = metadata
            cell_metadata_path.write_text(
                json.dumps(cell_metadata, indent=2) + "\n", encoding="utf-8"
            )

        summary_paths.append(jaeger / "derived_repetition_summary.csv")
        sample_paths.append(jaeger / "derived_samples.csv")
        quality_paths.append(jaeger / "data_quality.csv")

    combined_summary = root / "all_jaeger_derived_repetition_summaries.csv"
    combined_samples = root / "all_jaeger_derived_samples.csv"
    combined_quality = root / "analysis" / "data_quality.csv"
    combine(summary_paths, combined_summary)
    combine(sample_paths, combined_samples)
    combine(quality_paths, combined_quality)
    generate_jaeger_analysis(combined_summary, analysis, combined_samples)
    write_campaign_report(root, combined_quality, len(cells))
    print(f"reanalyzed {root}: {len(cells)} repetitions")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", type=Path, help="Campaign result directories")
    args = parser.parse_args()
    for root in args.results:
        reanalyze_campaign(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
