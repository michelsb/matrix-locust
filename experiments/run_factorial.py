#!/usr/bin/env python3
"""Run configurable Matrix load-test campaigns and build sampled datasets."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import random
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median


LOADS = (50, 100, 150)
WORKLOADS = ("text_only", "text_and_image")
JAEGER_METRICS = (
    "request_authentication_latency",
    "replication_dispatch_latency",
    "event_persister_pre_transaction_latency",
    "event_persister_processing_latency",
    "event_persistence_transaction_latency",
    "generic_worker_processing_latency",
    "postgresql_session_verification_latency",
    "nginx_total_request_latency",
    "nginx_worker_connection_latency",
    "nginx_upstream_first_byte_latency",
    "nginx_upstream_processing_latency",
    "nginx_upstream_response_transfer_latency",
    "nginx_proxy_client_overhead",
    # Deprecated alias retained so old experiment commands remain reproducible.
    "nginx_proxy_overhead",
)
CAMPAIGN_LOG_PATH: Path | None = None
SYNC_ENDPOINT = "/_matrix/client/v3/sync"
LOCUST_ENDPOINTS = {
    "sync": SYNC_ENDPOINT,
    "text_send": "/_matrix/client/v3/rooms/_/send/m.text",
    "image_send": "/_matrix/client/v3/rooms/_/send/m.image",
    "media_upload": "/_matrix/media/v3/upload",
}

# Prometheus metrics added to each sampled row. Every expression returns one
# homeserver-wide value; CPU additionally remains available per Synapse job.
SYNAPSE_METRICS = {
    "memory_total_mib": 'sum(process_resident_memory_bytes{instance="INSTANCE"}) / 1024 / 1024',
    "synapse_rps": 'sum(rate(synapse_http_server_response_count_total{instance="INSTANCE"}[WINDOW]))',
    "synapse_http_errors_per_second": 'sum(rate(synapse_http_server_response_time_seconds_count{instance="INSTANCE",code=~"4..|5.."}[WINDOW]))',
    "synapse_http_p95_ms": 'histogram_quantile(0.95, sum by (le) (rate(synapse_http_server_response_time_seconds_bucket{instance="INSTANCE"}[WINDOW]))) * 1000',
    "reactor_tick_p95_ms": 'histogram_quantile(0.95, sum by (le) (rate(python_twisted_reactor_tick_time_bucket{instance="INSTANCE"}[WINDOW]))) * 1000',
    "db_query_p95_ms": 'histogram_quantile(0.95, sum by (le) (rate(synapse_storage_query_time_bucket{instance="INSTANCE"}[WINDOW]))) * 1000',
    "db_schedule_p95_ms": 'histogram_quantile(0.95, sum by (le) (rate(synapse_storage_schedule_time_bucket{instance="INSTANCE"}[WINDOW]))) * 1000',
    "events_persisted_per_second": 'sum(rate(synapse_storage_events_persisted_events_total{instance="INSTANCE"}[WINDOW]))',
    "db_threadpool_utilization_percent": 'max(synapse_threadpool_working_threads{instance="INSTANCE",name="database-master"} / clamp_min(synapse_threadpool_total_threads{instance="INSTANCE",name="database-master"}, 1)) * 100',
    "replication_events_queue": 'max(synapse_replication_tcp_command_queue{instance="INSTANCE",stream_name="events"})',
    "notifier_users": 'sum(synapse_notifier_users{instance="INSTANCE"})',
    "open_fds_percent": 'max(process_open_fds{instance="INSTANCE"} / process_max_fds{instance="INSTANCE"}) * 100',
    "gc_time_ms_per_second": 'sum(rate(python_gc_time_sum{instance="INSTANCE"}[WINDOW])) * 1000',
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Matrix base URL")
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9091")
    parser.add_argument("--prometheus-timeout", type=float, default=30.0,
                        help="Timeout in seconds for each Prometheus HTTP request")
    parser.add_argument("--prometheus-retries", type=int, default=3,
                        help="Retries after a failed Prometheus HTTP request")
    parser.add_argument("--no-prometheus", dest="collect_prometheus", action="store_false",
                        help="Run with Locust metrics only; skip Prometheus entirely")
    parser.set_defaults(collect_prometheus=True)
    parser.add_argument("--instance", default="matrix-test.atlab.ufc.br")
    parser.add_argument("--spawn-rate", type=float, default=5.0)
    parser.add_argument("--message-rate", type=float, default=0.2,
                        help="Mean foreground actions/s per user")
    parser.add_argument("--image-ratio", type=float, default=0.15,
                        help="Image fraction in text_and_image workload")
    parser.add_argument("--text-length-profile", choices=("fixed", "short", "mixed", "long"),
                        default="fixed", help="Distribution used for text message length")
    parser.add_argument("--text-length-words", type=int, default=10,
                        help="Words per message when --text-length-profile=fixed")
    parser.add_argument("--sync-timeout", type=float, default=30.0,
                        help="Matrix long-poll /sync timeout in seconds")
    parser.add_argument("--max-throughput", action="store_true",
                        help="Disable pacing and run each user without waiting")
    parser.add_argument("--stabilization", type=int, default=60,
                        help="Seconds at full load before measurement")
    parser.add_argument("--measurement-duration", type=int, default=120,
                        help="Seconds included in the measured window")
    parser.add_argument("--collection-buffer", type=int, default=5,
                        help="Unmeasured tail used to flush final metric rows")
    parser.add_argument("--samples", type=int, default=31)
    parser.add_argument("--cpu-rate-window", default="30s")
    parser.add_argument("--jaeger-url", help="Jaeger Query base URL; omitted disables tracing collection")
    parser.add_argument("--no-jaeger", dest="collect_jaeger", action="store_false",
                        help="Disable Jaeger collection even when --jaeger-url is present")
    parser.set_defaults(collect_jaeger=True)
    parser.add_argument("--jaeger-service-prefix",
                        help="Collect services beginning with this value (default: --instance)")
    parser.add_argument("--jaeger-services", nargs="+", dest="jaeger_requested_services",
                        help="Exact service names to collect instead of prefix discovery")
    parser.add_argument("--jaeger-operations", nargs="+",
                        default=None,
                        help="Internal span operations to aggregate")
    parser.add_argument("--jaeger-metrics", nargs="+", choices=JAEGER_METRICS,
                        default=[], help="Named metrics; required services/operations are automatic")
    parser.add_argument("--jaeger-query-padding", type=int, default=30,
                        help="Seconds queried around the window; spans are still filtered exactly")
    parser.add_argument("--jaeger-chunk-duration", type=int, default=300,
                        help="Seconds per Jaeger trace query")
    parser.add_argument("--jaeger-query-limit", type=int, default=1000)
    parser.add_argument("--jaeger-min-chunk-duration", type=float, default=1.0,
                        help="Smallest automatic split for a saturated query, in seconds")
    parser.add_argument("--jaeger-workers", type=int, default=4,
                        help="Maximum concurrent Jaeger queries")
    parser.add_argument("--jaeger-timeout", type=float, default=30.0)
    parser.add_argument("--jaeger-retries", type=int, default=3)
    parser.add_argument("--jaeger-flush-wait", type=float, default=5.0,
                        help="Minimum wait after Locust stops before querying Jaeger")
    parser.add_argument("--jaeger-sampling-rate", type=float,
                        help="Configured trace sampling probability, recorded as provenance")
    parser.add_argument("--jaeger-include-db-statements", action="store_true",
                        help="Persist db.statement tags (may be large or sensitive)")
    parser.add_argument("--quality-warn-invalid-percent", type=float, default=5.0,
                        help="Warn when a metric exceeds this invalid-observation percentage")
    parser.add_argument("--quality-fail-invalid-percent", type=float, default=20.0,
                        help="Fail quality assessment above this invalid percentage")
    parser.add_argument("--quality-warn-t5-coverage-percent",
                        "--quality-warn-t4-coverage-percent",
                        dest="quality_warn_t4_coverage_percent", type=float, default=90.0,
                        help="Warn when T5 PostgreSQL-validation coverage is below this value")
    parser.add_argument("--quality-fail-t5-coverage-percent",
                        "--quality-fail-t4-coverage-percent",
                        dest="quality_fail_t4_coverage_percent", type=float, default=70.0,
                        help="Fail quality assessment when T5 coverage is below this value")
    parser.add_argument("--quality-warn-zero-percent", type=float, default=80.0,
                        help="Warn when this percentage is zero at NGINX timing resolution")
    parser.add_argument("--skip-analysis", action="store_true")
    parser.add_argument("--repetitions", type=int, default=3,
                        help="Independent executions per factorial cell")
    parser.add_argument("--cooldown", type=int, default=60,
                        help="Seconds between independent cells")
    parser.add_argument("--progress-interval", type=int, default=10,
                        help="Seconds between terminal progress updates")
    parser.add_argument("--seed", type=int, default=42, help="Seed used to randomize cell order")
    parser.add_argument("--output-dir", type=Path, default=Path("results"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/homeserver"),
                        help="Dataset containing users.csv and tokens.csv")
    parser.add_argument("--locustfile", type=Path, default=Path("locust-run-users.py"))
    parser.add_argument("--loads", type=int, nargs="+", default=list(LOADS))
    parser.add_argument("--workloads", nargs="+", choices=WORKLOADS, default=list(WORKLOADS))
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()
    if args.spawn_rate <= 0:
        parser.error("--spawn-rate must be greater than zero")
    if args.message_rate <= 0 and not args.max_throughput:
        parser.error("--message-rate must be > 0 unless --max-throughput is used")
    if not 0 < args.image_ratio < 1:
        parser.error("--image-ratio must be between 0 and 1 (exclusive)")
    if args.text_length_words < 1:
        parser.error("--text-length-words must be >= 1")
    if args.sync_timeout <= 0:
        parser.error("--sync-timeout must be > 0")
    if args.stabilization < 0 or args.measurement_duration <= 0 or args.collection_buffer < 0:
        parser.error("phase durations must be non-negative and measurement must be > 0")
    if args.samples < 2 or args.repetitions < 1 or args.cooldown < 0:
        parser.error("--samples must be >= 2 and --repetitions must be >= 1")
    if args.progress_interval < 1:
        parser.error("--progress-interval must be >= 1")
    if args.prometheus_timeout <= 0 or args.prometheus_retries < 0:
        parser.error("Prometheus timeout must be > 0 and retries must be >= 0")
    if args.jaeger_query_padding < 0 or args.jaeger_chunk_duration < 1:
        parser.error("Jaeger padding must be >= 0 and chunk duration must be >= 1")
    if args.jaeger_query_limit < 1 or args.jaeger_timeout <= 0 or args.jaeger_retries < 0:
        parser.error("Jaeger limit/timeout must be > 0 and retries must be >= 0")
    if args.jaeger_min_chunk_duration <= 0 or args.jaeger_workers < 1:
        parser.error("Jaeger minimum chunk and workers must be > 0")
    if args.jaeger_flush_wait < 0:
        parser.error("--jaeger-flush-wait must be >= 0")
    if args.jaeger_sampling_rate is not None and not 0 < args.jaeger_sampling_rate <= 1:
        parser.error("--jaeger-sampling-rate must be in (0, 1]")
    quality_values = (
        args.quality_warn_invalid_percent, args.quality_fail_invalid_percent,
        args.quality_warn_t4_coverage_percent, args.quality_fail_t4_coverage_percent,
        args.quality_warn_zero_percent,
    )
    if any(value < 0 or value > 100 for value in quality_values):
        parser.error("quality thresholds must be between 0 and 100")
    if args.quality_warn_invalid_percent > args.quality_fail_invalid_percent:
        parser.error("warning invalid threshold must not exceed fail threshold")
    if args.quality_fail_t4_coverage_percent > args.quality_warn_t4_coverage_percent:
        parser.error("fail T5 coverage threshold must not exceed warning threshold")
    args.collect_jaeger = bool(args.collect_jaeger and args.jaeger_url)
    args.jaeger_service_prefix = args.jaeger_service_prefix or args.instance
    if args.jaeger_operations is None:
        args.jaeger_operations = (
            [] if args.jaeger_metrics else ["db.query", "db.txn", "db.connection"]
        )
    return args


def format_duration(seconds: float) -> str:
    """Format a duration without hiding campaigns that last more than one hour."""
    seconds = max(0, round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def log_event(stage: str, message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{timestamp}] [{stage}] {message}"
    print(line, flush=True)
    if CAMPAIGN_LOG_PATH is not None:
        with CAMPAIGN_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def campaign_progress(args: argparse.Namespace) -> str:
    elapsed = time.time() - args.campaign_started
    overheads = list(args.campaign_cell_overheads)
    if args.campaign_current_cell_started is not None:
        current_elapsed = time.time() - args.campaign_current_cell_started
        current_overhead = max(0.0, current_elapsed - args.campaign_current_cell_base_seconds)
        if current_overhead:
            overheads.append(current_overhead)
    if overheads:
        projected = (
            args.campaign_base_estimated_seconds
            + sum(overheads) / len(overheads) * args.campaign_cell_total
        )
        # Never make the displayed finish time move backwards merely because a
        # later collection happened to be faster.
        args.campaign_estimated_seconds = max(args.campaign_estimated_seconds, projected)
    remaining = max(0, args.campaign_estimated_seconds - elapsed)
    percent = min(99.9, elapsed / args.campaign_estimated_seconds * 100) \
        if args.campaign_estimated_seconds else 100.0
    finish = datetime.fromtimestamp(time.time() + remaining).astimezone().strftime("%H:%M:%S")
    return (
        f"campanha {percent:5.1f}% | decorrido {format_duration(elapsed)} | "
        f"restante estimado {format_duration(remaining)} | término ~{finish}"
    )


def cell_phase(elapsed: float, ramp: int, stabilization: int,
               measurement: int, buffer: int) -> tuple[str, float, float]:
    phases = (
        ("ramp-up", ramp),
        ("estabilização", stabilization),
        ("medição", measurement),
        ("buffer", buffer),
    )
    phase_start = 0.0
    for name, duration in phases:
        phase_end = phase_start + duration
        if elapsed < phase_end:
            return name, min(max(0, elapsed - phase_start), duration), duration
        phase_start = phase_end
    return "finalização do Locust", elapsed - phase_start, 0


def wait_for_locust(process: subprocess.Popen, args: argparse.Namespace,
                    name: str, run_started: float, ramp: int) -> int:
    while True:
        elapsed = time.time() - run_started
        phase, phase_elapsed, phase_duration = cell_phase(
            elapsed, ramp, args.stabilization,
            args.measurement_duration, args.collection_buffer,
        )
        phase_progress = (
            f"{format_duration(phase_elapsed)}/{format_duration(phase_duration)}"
            if phase_duration else format_duration(phase_elapsed)
        )
        log_event(
            "progress",
            f"célula {args.campaign_cell_index}/{args.campaign_cell_total} {name} | "
            f"fase {phase} {phase_progress} | {campaign_progress(args)}",
        )
        try:
            return process.wait(timeout=args.progress_interval)
        except subprocess.TimeoutExpired:
            continue


def cooldown_with_progress(args: argparse.Namespace) -> None:
    deadline = time.time() + args.cooldown
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        log_event(
            "cooldown",
            f"próxima célula em {format_duration(remaining)} | {campaign_progress(args)}",
        )
        time.sleep(min(args.progress_interval, remaining))


def prometheus_query_range(base_url: str, query: str, start: float, end: float,
                           step: float, timeout: float = 30.0,
                           retries: int = 3) -> list[dict]:
    params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    url = f"{base_url.rstrip('/')}/api/v1/query_range?{params}"
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                payload = json.load(response)
            break
        except Exception as exc:
            last_error = exc
            if attempt == retries:
                raise RuntimeError(
                    f"Prometheus request failed after {retries + 1} attempts: {last_error}"
                ) from exc
            time.sleep(min(2 ** attempt, 5))
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    return payload["data"]["result"]


def read_history(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not any(row.get("Name") == "Aggregated" for row in rows):
        raise RuntimeError(f"No Locust aggregate rows found: {path}")
    return rows


def sample_history(rows: list[dict], start: float, end: float, count: int) -> list[dict]:
    rows = [row for row in rows if row.get("Name") == "Aggregated"]
    if not rows:
        raise RuntimeError("No Locust aggregate rows found")
    first_timestamp = min(float(row["Timestamp"]) for row in rows)
    last_timestamp = max(float(row["Timestamp"]) for row in rows)
    if first_timestamp > start:
        raise RuntimeError("Locust history started after the measurement window")
    if last_timestamp < end:
        raise RuntimeError("Locust history ended before the measurement window was complete")
    targets = [start + i * (end - start) / (count - 1) for i in range(count)]
    return [min(rows, key=lambda row: abs(float(row["Timestamp"]) - target)) for target in targets]


def numeric(row: dict, field: str) -> float:
    try:
        return float(row.get(field, 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def endpoint_rows_at(history: list[dict], timestamp: float) -> dict[str, dict]:
    endpoints = {}
    names = {row.get("Name") for row in history} - {None, "Aggregated"}
    for name in names:
        rows = [row for row in history if row.get("Name") == name]
        endpoints[name] = min(rows, key=lambda row: abs(float(row["Timestamp"]) - timestamp))
    return endpoints


def locust_breakdown(history: list[dict], timestamp: float) -> dict[str, float | str]:
    endpoints = endpoint_rows_at(history, timestamp)
    output = {}
    for prefix, name in LOCUST_ENDPOINTS.items():
        row = endpoints.get(name)
        output.update({
            f"{prefix}_rps": numeric(row, "Requests/s") if row else "",
            f"{prefix}_avg_response_time_ms": numeric(row, "Total Average Response Time") if row else "",
            f"{prefix}_p95_response_time_ms": numeric(row, "95%") if row else "",
        })

    foreground = [row for name, row in endpoints.items() if name != SYNC_ENDPOINT]
    output["foreground_rps"] = sum(numeric(row, "Requests/s") for row in foreground)
    output["foreground_failures_per_second"] = sum(
        numeric(row, "Failures/s") for row in foreground
    )
    weights = [numeric(row, "Requests/s") for row in foreground]
    weight_total = sum(weights)
    output["foreground_avg_response_time_ms"] = (
        sum(numeric(row, "Total Average Response Time") * weight
            for row, weight in zip(foreground, weights)) / weight_total
        if weight_total else ""
    )
    return output


def cpu_by_timestamp(series: list[dict]) -> dict[float, dict[str, float]]:
    output: dict[float, dict[str, float]] = {}
    for item in series:
        job = item.get("metric", {}).get("job", "unknown")
        for timestamp, value in item.get("values", []):
            output.setdefault(float(timestamp), {})[job] = float(value)
    return output


def nearest_cpu(cpu: dict[float, dict[str, float]], timestamp: float) -> dict[str, float]:
    if not cpu:
        return {}
    return cpu[min(cpu, key=lambda candidate: abs(candidate - timestamp))]


def scalar_by_timestamp(series: list[dict]) -> dict[float, float]:
    output: dict[float, float] = {}
    for item in series:
        for timestamp, value in item.get("values", []):
            numeric = float(value)
            if math.isfinite(numeric):
                output[float(timestamp)] = output.get(float(timestamp), 0.0) + numeric
    return output


def nearest_scalar(series: dict[float, float], timestamp: float):
    if not series:
        return ""
    return series[min(series, key=lambda candidate: abs(candidate - timestamp))]


def dataset_manifest(data_dir: Path) -> dict:
    files = {}
    for name in ("users.csv", "tokens.csv", "rooms.json", "rooms_status.csv"):
        path = data_dir / name
        if path.exists():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files[name] = {"path": str(path.resolve()), "sha256": digest, "bytes": path.stat().st_size}
    return {"data_dir": str(data_dir.resolve()), "files": files}


def execution_provenance() -> dict:
    """Capture enough local context to reproduce and audit a campaign."""
    def git(*arguments: str) -> str | None:
        try:
            return subprocess.run(
                ["git", *arguments], check=True, capture_output=True,
                text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    try:
        locust_version = importlib.metadata.version("locust")
    except importlib.metadata.PackageNotFoundError:
        locust_version = None
    status = git("status", "--porcelain")
    return {
        "argv": sys.argv,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "locust_version": locust_version,
        "git_commit": git("rev-parse", "HEAD"),
        "git_dirty": bool(status) if status is not None else None,
    }


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def process_message_arrivals(path: Path, measure_start: float, measure_end: float,
                             samples: int, metadata: dict, cell_dir: Path
                             ) -> tuple[dict[int, float], dict]:
    """Calculate global message injection inter-arrivals on a monotonic clock."""
    if not path.is_file():
        raise RuntimeError(f"Locust did not produce mandatory T1 arrivals: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    window = [
        row for row in rows
        if measure_start <= int(row["epoch_ns"]) / 1_000_000_000 < measure_end
    ]
    window.sort(key=lambda row: int(row["monotonic_ns"]))
    observations = []
    by_sample: dict[int, list[float]] = {}
    step = (measure_end - measure_start) / (samples - 1)
    for previous, current in zip(window, window[1:]):
        interval_ms = (
            int(current["monotonic_ns"]) - int(previous["monotonic_ns"])
        ) / 1_000_000
        current_epoch = int(current["epoch_ns"]) / 1_000_000_000
        sample = max(1, min(samples, round((current_epoch - measure_start) / step) + 1))
        by_sample.setdefault(sample, []).append(interval_ms)
        observations.append({
            "users": metadata["users"], "workload": metadata["workload"],
            "repetition": metadata["repetition"], "sample": sample,
            "timestamp": datetime.fromtimestamp(current_epoch, timezone.utc).isoformat(),
            "interval_ms": interval_ms, "message_type": current["message_type"],
            "user": current["user"], "previous_sequence": previous["sequence"],
            "sequence": current["sequence"],
        })
    if not observations:
        raise RuntimeError("T1 has no consecutive message injections inside the measurement window")
    fields = (
        "users", "workload", "repetition", "sample", "timestamp", "interval_ms",
        "message_type", "user", "previous_sequence", "sequence",
    )
    with (cell_dir / "message_interarrival_observations.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(observations)
    values = [row["interval_ms"] for row in observations]
    summary = {
        "users": metadata["users"], "workload": metadata["workload"],
        "repetition": metadata["repetition"], "count": len(values),
        "mean_ms": mean(values), "median_ms": median(values),
        "p95_ms": percentile(values, 0.95), "p99_ms": percentile(values, 0.99),
        "max_ms": max(values),
    }
    with (cell_dir / "message_interarrival_summary.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=summary.keys())
        writer.writeheader()
        writer.writerow(summary)
    return ({sample: mean(values) for sample, values in by_sample.items()}, summary)


def request_scope(endpoint: str) -> str:
    """Map a normalized Locust endpoint to an analysis scope."""
    if endpoint.endswith("/sync"):
        return "sync"
    if "send/m.text" in endpoint:
        return "text_send"
    if "send/m.image" in endpoint:
        return "image_send"
    if "/media/" in endpoint and endpoint.endswith("/upload"):
        return "media_upload"
    return "other"


def process_request_arrivals(path: Path, measure_start: float, measure_end: float,
                             samples: int, metadata: dict, cell_dir: Path
                             ) -> tuple[dict[str, dict[int, float]], list[dict]]:
    """Calculate request-start inter-arrivals for all and endpoint scopes."""
    if not path.is_file():
        raise RuntimeError(f"Locust did not produce request arrivals: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle)
                if measure_start <= int(row["epoch_ns"]) / 1_000_000_000 < measure_end]
    for row in rows:
        row["endpoint_scope"] = request_scope(row["endpoint"])
    scopes = {
        "all": rows,
        "foreground": [row for row in rows if row["endpoint_scope"] != "sync"],
        **{scope: [row for row in rows if row["endpoint_scope"] == scope]
           for scope in ("sync", "text_send", "image_send", "media_upload", "other")},
    }
    step = (measure_end - measure_start) / (samples - 1)
    observations = []
    summaries = []
    sample_values: dict[str, dict[int, list[float]]] = {}
    for scope, scoped_rows in scopes.items():
        scoped_rows.sort(key=lambda row: int(row["monotonic_ns"]))
        for previous, current in zip(scoped_rows, scoped_rows[1:]):
            interval_ms = (int(current["monotonic_ns"]) - int(previous["monotonic_ns"])) / 1_000_000
            current_epoch = int(current["epoch_ns"]) / 1_000_000_000
            sample = max(1, min(samples, round((current_epoch - measure_start) / step) + 1))
            sample_values.setdefault(scope, {}).setdefault(sample, []).append(interval_ms)
            observations.append({
                "users": metadata["users"], "workload": metadata["workload"],
                "repetition": metadata["repetition"], "scope": scope, "sample": sample,
                "timestamp": datetime.fromtimestamp(current_epoch, timezone.utc).isoformat(),
                "interval_ms": interval_ms, "method": current["method"],
                "endpoint": current["endpoint"], "user": current["user"],
                "previous_sequence": previous["sequence"], "sequence": current["sequence"],
            })
        values = [row["interval_ms"] for row in observations if row["scope"] == scope]
        if values:
            summaries.append({
                "users": metadata["users"], "workload": metadata["workload"],
                "repetition": metadata["repetition"], "scope": scope, "count": len(values),
                "mean_ms": mean(values), "median_ms": median(values),
                "p95_ms": percentile(values, .95), "p99_ms": percentile(values, .99),
                "max_ms": max(values),
            })
    observation_fields = ("users", "workload", "repetition", "scope", "sample", "timestamp",
                          "interval_ms", "method", "endpoint", "user",
                          "previous_sequence", "sequence")
    with (cell_dir / "request_interarrival_observations.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=observation_fields)
        writer.writeheader()
        writer.writerows(observations)
    summary_fields = ("users", "workload", "repetition", "scope", "count", "mean_ms",
                      "median_ms", "p95_ms", "p99_ms", "max_ms")
    with (cell_dir / "request_interarrival_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=summary_fields)
        writer.writeheader()
        writer.writerows(summaries)
    means = {scope: {sample: mean(values) for sample, values in grouped.items()}
             for scope, grouped in sample_values.items()}
    return means, summaries


def write_samples(path: Path, locust_rows: list[dict], locust_history: list[dict],
                  cpu: dict[float, dict[str, float]],
                  synapse_metrics: dict[str, dict[float, float]], metadata: dict,
                  message_interarrival_by_sample: dict[int, float],
                  request_interarrival_by_scope: dict[str, dict[int, float]]) -> None:
    jobs = sorted({job for values in cpu.values() for job in values})
    include_prometheus = bool(cpu) or bool(synapse_metrics)
    fields = [
        "sample", "timestamp", "users", "workload", "repetition", "rps",
        "failures_per_second", "avg_response_time_ms", "median_response_time_ms",
        "p95_response_time_ms", "message_interarrival_time_ms",
        *[f"request_interarrival_{scope}_ms" for scope in
          ("all", "foreground", "sync", "text_send", "image_send", "media_upload", "other")],
        "foreground_rps", "foreground_failures_per_second",
        "foreground_avg_response_time_ms",
        *[f"{prefix}_{suffix}" for prefix in LOCUST_ENDPOINTS
          for suffix in ("rps", "avg_response_time_ms", "p95_response_time_ms")],
    ]
    if include_prometheus:
        fields.extend(["cpu_total_percent", *SYNAPSE_METRICS])
        fields.extend(f"cpu_{job}_percent" for job in jobs)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, locust_row in enumerate(locust_rows, 1):
            timestamp = float(locust_row["Timestamp"])
            cpu_values = nearest_cpu(cpu, timestamp)
            row = {
                "sample": index,
                "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                "users": metadata["users"],
                "workload": metadata["workload"],
                "repetition": metadata["repetition"],
                "rps": locust_row.get("Requests/s", ""),
                "failures_per_second": locust_row.get("Failures/s", ""),
                "avg_response_time_ms": locust_row.get("Total Average Response Time", ""),
                "median_response_time_ms": locust_row.get("Total Median Response Time", ""),
                "p95_response_time_ms": locust_row.get("95%", ""),
                "message_interarrival_time_ms": message_interarrival_by_sample.get(index, ""),
            }
            row.update({f"request_interarrival_{scope}_ms": values.get(index, "")
                        for scope, values in request_interarrival_by_scope.items()})
            row.update(locust_breakdown(locust_history, timestamp))
            if include_prometheus:
                row["cpu_total_percent"] = sum(cpu_values.values())
                row.update({f"cpu_{job}_percent": cpu_values.get(job, "") for job in jobs})
                row.update({name: nearest_scalar(synapse_metrics.get(name, {}), timestamp)
                            for name in SYNAPSE_METRICS})
            writer.writerow(row)


def cell_is_complete(args: argparse.Namespace, cell_dir: Path) -> bool:
    """Return true only when every collector requested for this run completed."""
    metadata_path = cell_dir / "metadata.json"
    required = (
        cell_dir / "samples.csv",
        cell_dir / "message_interarrival_summary.csv",
        cell_dir / "request_interarrival_summary.csv",
    )
    if not all(path.is_file() for path in required) or not metadata_path.is_file():
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if metadata.get("cell_status") != "complete":
        return False
    if args.collect_jaeger and metadata.get("jaeger_collection", {}).get("status") != "complete":
        return False
    return True


def run_cell(args: argparse.Namespace, users: int, workload: str, repetition: int) -> Path:
    name = f"users-{users}__{workload}__rep-{repetition:02d}"
    cell_dir = args.output_dir / name
    samples_path = cell_dir / "samples.csv"
    interarrival_summary_path = cell_dir / "message_interarrival_summary.csv"
    request_interarrival_summary_path = cell_dir / "request_interarrival_summary.csv"
    if args.skip_existing and cell_is_complete(args, cell_dir):
        log_event("skip", f"célula já concluída: {name}")
        return samples_path
    if args.skip_existing and samples_path.exists():
        log_event("resume", f"{name}: célula antiga sem chegadas completas será reexecutada")
    cell_dir.mkdir(parents=True, exist_ok=True)
    prefix = cell_dir / "locust"
    env = os.environ.copy()
    env["MATRIX_WORKLOAD"] = workload
    env["MATRIX_DATA_DIR"] = str(args.data_dir.resolve())
    env["MATRIX_MESSAGE_RATE"] = "0" if args.max_throughput else str(args.message_rate)
    env["MATRIX_IMAGE_RATIO"] = str(args.image_ratio)
    env["MATRIX_TEXT_LENGTH_PROFILE"] = args.text_length_profile
    env["MATRIX_TEXT_LENGTH_WORDS"] = str(args.text_length_words)
    env["MATRIX_SYNC_TIMEOUT_MS"] = str(round(args.sync_timeout * 1000))
    # Keep the setup dataset immutable across randomized factorial cells.
    env["MATRIX_PERSIST_TOKENS"] = "false"
    cell_seed_material = f"{args.seed}:{users}:{workload}:{repetition}".encode("utf-8")
    cell_seed = int.from_bytes(hashlib.sha256(cell_seed_material).digest()[:8], "big")
    env["MATRIX_EXPERIMENT_SEED"] = str(cell_seed)
    workload_stats_path = cell_dir / "workload_stats.json"
    env["MATRIX_WORKLOAD_STATS_PATH"] = str(workload_stats_path.resolve())
    message_arrivals_path = cell_dir / "message_arrivals.csv"
    env["MATRIX_MESSAGE_ARRIVALS_PATH"] = str(message_arrivals_path.resolve())
    request_arrivals_path = cell_dir / "request_arrivals.csv"
    env["MATRIX_REQUEST_ARRIVALS_PATH"] = str(request_arrivals_path.resolve())
    expected_ramp_seconds = math.ceil(users / args.spawn_rate)
    run_time_seconds = (
        expected_ramp_seconds + args.stabilization
        + args.measurement_duration + args.collection_buffer
    )
    cell_base_seconds = run_time_seconds + (
        args.jaeger_flush_wait if args.collect_jaeger else 0
    )
    args.campaign_current_cell_started = time.time()
    args.campaign_current_cell_base_seconds = cell_base_seconds
    command = [
        "locust", "-f", str(args.locustfile), "--headless", "--host", args.host,
        "--users", str(users), "--spawn-rate", str(args.spawn_rate),
        "--run-time", f"{run_time_seconds}s", "--csv", str(prefix),
        "--csv-full-history", "--html", str(cell_dir / "report.html"),
        # HTTP failures are experimental observations, not runner failures.
        # They remain available in Locust CSVs and sampled failure metrics.
        "--exit-code-on-error", "0",
    ]
    started = time.time()
    metadata = {
        "name": name, "users": users, "workload": workload, "repetition": repetition,
        "spawn_rate": args.spawn_rate, "expected_ramp_seconds": expected_ramp_seconds,
        "load_mode": "max_throughput" if args.max_throughput else "paced",
        "message_rate_per_user": 0 if args.max_throughput else args.message_rate,
        "expected_interval_seconds": None if args.max_throughput else 1 / args.message_rate,
        "image_ratio": args.image_ratio if workload == "text_and_image" else 0,
        "text_length_profile": args.text_length_profile,
        "text_length_words": args.text_length_words if args.text_length_profile == "fixed" else None,
        "experiment_seed": cell_seed,
        "sync_timeout_seconds": args.sync_timeout,
        "persist_tokens": False,
        "stabilization_seconds": args.stabilization,
        "measurement_duration_seconds": args.measurement_duration,
        "collection_buffer_seconds": args.collection_buffer,
        "collect_prometheus": args.collect_prometheus,
        "prometheus_url": args.prometheus_url,
        "prometheus_timeout_seconds": args.prometheus_timeout,
        "prometheus_retries": args.prometheus_retries,
        "collect_jaeger": args.collect_jaeger,
        "jaeger_url": args.jaeger_url,
        "jaeger_service_prefix": args.jaeger_service_prefix,
        "jaeger_requested_services": args.jaeger_requested_services,
        "jaeger_operations": args.jaeger_operations,
        "jaeger_metrics": args.jaeger_metrics,
        "jaeger_query_padding_seconds": args.jaeger_query_padding,
        "jaeger_chunk_duration_seconds": args.jaeger_chunk_duration,
        "jaeger_min_chunk_duration_seconds": args.jaeger_min_chunk_duration,
        "jaeger_workers": args.jaeger_workers,
        "jaeger_flush_wait_seconds": args.jaeger_flush_wait,
        "jaeger_sampling_rate": args.jaeger_sampling_rate,
        "quality_thresholds": {
            "warn_invalid_percent": args.quality_warn_invalid_percent,
            "fail_invalid_percent": args.quality_fail_invalid_percent,
            "warn_t4_coverage_percent": args.quality_warn_t4_coverage_percent,
            "fail_t4_coverage_percent": args.quality_fail_t4_coverage_percent,
            "warn_zero_percent": args.quality_warn_zero_percent,
        },
        "run_time_seconds": run_time_seconds, "samples": args.samples,
        "started_epoch": started, "command": command, "cell_status": "running",
        "dataset": args.dataset_manifest,
        "execution_provenance": args.execution_provenance,
    }
    (cell_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    log_event(
        "cell",
        f"iniciando {args.campaign_cell_index}/{args.campaign_cell_total}: {name} | "
        f"duração estimada {format_duration(run_time_seconds)} | {campaign_progress(args)}",
    )
    with (cell_dir / "locust.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        previous_sigterm_handler = signal.getsignal(signal.SIGTERM)

        def handle_sigterm(_signum: int, _frame: object) -> None:
            """Turn supervisor termination into the same cleanup used by Ctrl+C."""
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, handle_sigterm)
        try:
            return_code = wait_for_locust(process, args, name, started, expected_ramp_seconds)
        except KeyboardInterrupt:
            log_event("interrupt", f"{name}: encerrando o Locust")
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log_event("interrupt", f"{name}: forçando o encerramento do Locust")
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)
    ended = time.time()
    if return_code != 0:
        raise RuntimeError(f"Locust failed ({return_code}); see {cell_dir / 'locust.log'}")
    if workload_stats_path.exists():
        metadata["workload_stats"] = json.loads(workload_stats_path.read_text(encoding="utf-8"))
        workload_stats = metadata["workload_stats"]
        foreground_attempts = (
            workload_stats.get("text", {}).get("attempts", 0)
            + workload_stats.get("image", {}).get("attempts", 0)
        )
        if foreground_attempts == 0:
            raise RuntimeError(
                f"Cell produced no foreground actions; see {cell_dir / 'locust.log'}"
            )
    else:
        log_event("warning", f"{name}: workload_stats.json não foi gerado pelo Locust")

    log_event("collect", f"{name}: Locust concluído; lendo histórico e janela experimental")
    all_history = read_history(Path(f"{prefix}_stats_history.csv"))
    aggregate_history = [row for row in all_history if row.get("Name") == "Aggregated"]
    if max(numeric(row, "Total Request Count") for row in aggregate_history) <= 0:
        raise RuntimeError(f"Cell produced zero Locust requests; see {cell_dir / 'locust.log'}")
    full_load_rows = [row for row in aggregate_history if int(row["User Count"]) >= users]
    if not full_load_rows:
        maximum = max(int(row["User Count"]) for row in aggregate_history)
        raise RuntimeError(
            f"Cell never reached the requested load of {users} users (maximum={maximum})"
        )
    full_load_at = min(float(row["Timestamp"]) for row in full_load_rows)
    measure_start = full_load_at + args.stabilization
    measure_end = measure_start + args.measurement_duration
    history = sample_history(all_history, measure_start, measure_end, args.samples)
    message_interarrival_by_sample, message_interarrival_summary = process_message_arrivals(
        message_arrivals_path, measure_start, measure_end, args.samples, metadata, cell_dir
    )
    metadata["message_interarrival"] = {
        "metric": "message_interarrival_time_ms",
        "definition": "global interval between consecutive Locust room_send injections",
        "clock": "time.monotonic_ns",
        "window_rule": "both injections must be inside the measurement window",
        **message_interarrival_summary,
    }
    request_interarrival_by_scope, request_interarrival_summaries = process_request_arrivals(
        request_arrivals_path, measure_start, measure_end, args.samples, metadata, cell_dir
    )
    metadata["request_interarrival"] = {
        "definition": "interval between consecutive Locust HTTP request starts within each scope",
        "clock": "time.monotonic_ns", "canonical_t1": False,
        "foreground_rule": "all request starts except /sync",
        "summaries": request_interarrival_summaries,
    }
    step = (measure_end - measure_start) / (args.samples - 1)
    query = None
    cpu_series = []
    observed_jobs = []
    metric_queries = {}
    synapse_metrics = {}
    if args.collect_prometheus:
        query = (
            f'sum by (job) (rate(process_cpu_seconds_total{{instance="{args.instance}"}}'
            f'[{args.cpu_rate_window}])) * 100'
        )
        log_event("collect", f"{name}: consultando CPU no Prometheus")
        cpu_series = prometheus_query_range(
            args.prometheus_url, query, measure_start, measure_end, step,
            args.prometheus_timeout, args.prometheus_retries,
        )
        if not cpu_series:
            raise RuntimeError(
                "Prometheus returned no process_cpu_seconds_total series for "
                f"instance={args.instance!r}"
            )
        observed_jobs = sorted({
            item.get("metric", {}).get("job", "unknown") for item in cpu_series
        })
        log_event(
            "collect",
            f"{name}: {len(observed_jobs)} jobs com CPU: {', '.join(observed_jobs)}",
        )
        metric_queries = {
            metric_name: template.replace("INSTANCE", args.instance).replace(
                "WINDOW", args.cpu_rate_window
            )
            for metric_name, template in SYNAPSE_METRICS.items()
        }
        for metric_index, (metric_name, metric_query) in enumerate(metric_queries.items(), 1):
            log_event(
                "collect",
                f"{name}: métrica Synapse {metric_index}/{len(metric_queries)} ({metric_name})",
            )
            series = prometheus_query_range(
                args.prometheus_url, metric_query, measure_start, measure_end, step,
                args.prometheus_timeout, args.prometheus_retries,
            )
            synapse_metrics[metric_name] = scalar_by_timestamp(series)
            if not synapse_metrics[metric_name]:
                log_event("warning", f"Prometheus não retornou valores para {metric_name}")
    else:
        log_event("collect", f"{name}: coleta do Prometheus desabilitada")
    write_samples(
        samples_path, history, all_history, cpu_by_timestamp(cpu_series), synapse_metrics,
        metadata, message_interarrival_by_sample, request_interarrival_by_scope,
    )
    jaeger_metadata = {"enabled": args.collect_jaeger, "status": "disabled"}
    if args.collect_jaeger:
        from jaeger_collector import (
            aggregate_derived, aggregate_spans, collect_spans, derive_metrics,
            metric_formulas, write_jaeger_artifacts, write_quality_artifacts,
        )

        remaining_flush_wait = max(0.0, args.jaeger_flush_wait - (time.time() - ended))
        if remaining_flush_wait:
            log_event(
                "collect",
                f"{name}: aguardando {format_duration(remaining_flush_wait)} pelo flush do Jaeger",
            )
            time.sleep(remaining_flush_wait)
        log_event(
            "collect",
            f"{name}: consultando {len(args.jaeger_services)} serviços no Jaeger",
        )
        jaeger_dir = cell_dir / "jaeger"
        try:
            collection_operations = list(dict.fromkeys([
                *args.jaeger_operations,
                *args.jaeger_planned_operations,
            ]))
            spans, jaeger_metadata, raw_traces = collect_spans(
                args.jaeger_url, args.jaeger_services, collection_operations,
                measure_start, measure_end, args.samples,
                args.jaeger_chunk_duration, args.jaeger_query_padding,
                args.jaeger_query_limit, args.jaeger_min_chunk_duration,
                args.jaeger_workers, args.jaeger_timeout, args.jaeger_retries,
                args.jaeger_include_db_statements,
                lambda message: log_event("warning", f"{name}: {message}"),
                lambda message: log_event(
                    "jaeger", f"{name}: {message} | {campaign_progress(args)}"
                ),
            )
            sample_timestamps = [
                datetime.fromtimestamp(float(row["Timestamp"]), timezone.utc).isoformat()
                for row in history
            ]
            jaeger_samples, jaeger_summary = aggregate_spans(
                spans, metadata, sample_timestamps
            )
            derived_observations = derived_samples = derived_summary = None
            if args.jaeger_metrics:
                derived_observations = derive_metrics(
                    raw_traces, args.jaeger_metrics,
                    measure_start, measure_end, args.samples, metadata,
                )
                derived_samples, derived_summary = aggregate_derived(
                    derived_observations, metadata, sample_timestamps
                )
                observation_counts = {}
                for metric in args.jaeger_metrics:
                    metric_rows = [
                        row for row in derived_observations if row["metric"] == metric
                    ]
                    if metric == "event_persister_processing_latency":
                        summaries = [
                            row for row in derived_summary or []
                            if row["operation"] == metric
                            and row.get("scope") == "message_send"
                        ]
                        observation_counts[metric] = {
                            "valid": len(summaries), "invalid": 0,
                            "unit": "run_level_estimate",
                        }
                    else:
                        observation_counts[metric] = {
                            "valid": sum(row["valid"] is True for row in metric_rows),
                            "invalid": sum(row["valid"] is not True for row in metric_rows),
                            "unit": "span_observation",
                        }
                jaeger_metadata["derived_observation_counts"] = observation_counts
            jaeger_metadata.update({
                "enabled": True, "status": "complete",
                "url": args.jaeger_url, "service_prefix": args.jaeger_service_prefix,
                "services": args.jaeger_services, "operations": args.jaeger_operations,
                "collection_operations": collection_operations,
                "derived_metrics": args.jaeger_metrics,
                "derived_metric_formulas": metric_formulas(args.jaeger_metrics),
                "query_padding_seconds": args.jaeger_query_padding,
                "chunk_duration_seconds": args.jaeger_chunk_duration,
                "minimum_chunk_duration_seconds": args.jaeger_min_chunk_duration,
                "workers": args.jaeger_workers,
                "query_limit": args.jaeger_query_limit,
                "include_db_statements": args.jaeger_include_db_statements,
            })
            if derived_observations is not None:
                quality_report = write_quality_artifacts(
                    jaeger_dir, derived_observations, derived_summary or [],
                    {**jaeger_metadata, **{
                        key: metadata[key] for key in ("users", "workload", "repetition")
                    }}, metadata["quality_thresholds"],
                )
                jaeger_metadata["data_quality"] = {
                    "overall": quality_report["overall"],
                    "thresholds": quality_report["thresholds"],
                }
            write_jaeger_artifacts(
                jaeger_dir, spans, jaeger_samples, jaeger_summary, jaeger_metadata,
                derived_observations, derived_samples, derived_summary,
            )
            log_event(
                "collect",
                f"{name}: Jaeger concluiu com {len(spans)} spans únicos",
            )
        except Exception as exc:
            jaeger_metadata = {
                "enabled": True, "status": "failed", "error": str(exc),
                "url": args.jaeger_url, "services": args.jaeger_services,
                "operations": args.jaeger_operations,
                "measurement_start_epoch": measure_start,
                "measurement_end_epoch": measure_end,
            }
            write_jaeger_artifacts(
                jaeger_dir, [], [], [], jaeger_metadata,
                *([[], [], []] if args.jaeger_metrics else []),
            )
            log_event(
                "warning",
                f"{name}: coleta do Jaeger falhou; resultados Locust/Prometheus preservados: {exc}",
            )
    else:
        log_event("collect", f"{name}: coleta do Jaeger desabilitada")
    metadata.update({
        "ended_epoch": ended,
        "full_load_epoch": full_load_at,
        "actual_ramp_seconds": full_load_at - min(float(row["Timestamp"]) for row in aggregate_history),
        "measurement_start_epoch": measure_start,
        "measurement_end_epoch": measure_end,
        "promql": query,
        "observed_prometheus_jobs": observed_jobs,
        "synapse_metric_queries": metric_queries,
        "jaeger_collection": jaeger_metadata,
        "cell_status": (
            "complete" if not args.collect_jaeger or jaeger_metadata.get("status") == "complete"
            else "partial_jaeger_failed"
        ),
    })
    (cell_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    actual_cell_seconds = time.time() - args.campaign_current_cell_started
    args.campaign_cell_overheads.append(max(0.0, actual_cell_seconds - cell_base_seconds))
    args.campaign_current_cell_started = None
    log_event("cell", f"concluída: {name} | amostras em {samples_path} | {campaign_progress(args)}")
    return samples_path


def combine(paths: list[Path], output: Path) -> None:
    rows: list[dict] = []
    fields: list[str] = []
    for path in paths:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for field in reader.fieldnames or []:
                if field not in fields:
                    fields.append(field)
            rows.extend(reader)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_collection_status(paths: list[Path], output: Path) -> list[dict]:
    """Persist collector completeness so missing repetitions cannot be silent."""
    rows = []
    for samples_path in paths:
        metadata_path = samples_path.parent / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        jaeger = metadata.get("jaeger_collection", {})
        rows.append({
            "name": metadata.get("name", samples_path.parent.name),
            "users": metadata.get("users", ""),
            "workload": metadata.get("workload", ""),
            "repetition": metadata.get("repetition", ""),
            "cell_status": metadata.get("cell_status", "unknown"),
            "locust_status": "complete" if samples_path.is_file() else "missing",
            "prometheus_status": (
                "complete" if metadata.get("collect_prometheus") else "disabled"
            ),
            "jaeger_status": jaeger.get("status", "disabled"),
            "jaeger_error": jaeger.get("error", ""),
        })
    fields = (
        "name", "users", "workload", "repetition", "cell_status",
        "locust_status", "prometheus_status", "jaeger_status", "jaeger_error",
    )
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_campaign_report(output_dir: Path, quality_csv: Path,
                          cell_count: int) -> None:
    """Create a human-readable campaign summary without hiding failed metrics."""
    with quality_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    severity = {"PASS": 0, "WARNING": 1, "FAIL": 2}
    overall = max((row["quality"] for row in rows),
                  key=lambda item: severity.get(item, 2), default="FAIL")
    counts = {
        status: sum(row["quality"] == status for row in rows)
        for status in severity
    }
    lines = [
        "# Experiment report", "", f"Overall data quality: **{overall}**", "",
        "## Campaign", "", f"- Completed cells: {cell_count}",
        f"- Metric assessments: {len(rows)}",
        f"- PASS: {counts['PASS']}", f"- WARNING: {counts['WARNING']}",
        f"- FAIL: {counts['FAIL']}", "", "## Quality by run and metric", "",
        "| Users | Workload | Repetition | Metric | Valid | Invalid % | Coverage % | Quality | Reason |",
        "|---:|---|---:|---|---:|---:|---:|---|---|",
    ]
    for row in rows:
        coverage = row.get("coverage_percent") or "—"
        lines.append(
            f"| {row['users']} | {row['workload']} | {row['repetition']} | "
            f"`{row['metric']}` | {row['valid_observations']} | "
            f"{float(row['invalid_percent']):.1f} | {coverage} | "
            f"{row['quality']} | {row.get('reason') or '—'} |"
        )
    lines.extend([
        "", "## Artifacts", "",
        "- [Consolidated data quality](analysis/data_quality.csv)",
        "- [Jaeger confidence intervals](analysis/jaeger-derived/confidence_intervals.csv)",
        "- [Jaeger plots](analysis/jaeger-derived/plots/)",
        "- [Skipped inference](analysis/jaeger-derived/skipped_inference.csv)",
        "- [All derived run summaries](all_jaeger_derived_repetition_summaries.csv)",
        "- [Message inter-arrival summaries (T1)](all_message_interarrival_summaries.csv)",
        "- [Message inter-arrival observations (T1)](all_message_interarrival_observations.csv)",
        "- [Request inter-arrival summaries by scope](all_request_interarrival_summaries.csv)",
        "- [Request inter-arrival observations by scope](all_request_interarrival_observations.csv)",
        "", "## Statistical interpretation", "",
        "Spans and temporal samples are not independent replicates. Confidence "
        "intervals, factorial ANOVA and Tukey HSD use one run-level summary per "
        "scenario and repetition. Metrics marked FAIL must not be used for "
        "inference until their data-quality problem is corrected.", "",
    ])
    (output_dir / "EXPERIMENT_REPORT.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def main() -> int:
    global CAMPAIGN_LOG_PATH
    args = parse_args()
    if not shutil.which("locust"):
        sys.exit("locust not found in PATH; activate the Poetry/virtualenv environment")
    if args.workloads and "text_and_image" in args.workloads and not list(Path("images").glob("*.jpg")):
        sys.exit("text_and_image requires at least one JPG file in images/")
    args.data_dir = args.data_dir.resolve()
    missing = [name for name in ("users.csv", "tokens.csv") if not (args.data_dir / name).is_file()]
    if missing:
        sys.exit(f"Dataset {args.data_dir} is missing: {', '.join(missing)}")
    args.dataset_manifest = dataset_manifest(args.data_dir)
    args.execution_provenance = execution_provenance()
    # Fail before a long campaign only when remote metrics were requested.
    if args.collect_prometheus:
        try:
            prometheus_query_range(
                args.prometheus_url, "up", time.time() - 10, time.time(), 10,
                args.prometheus_timeout, args.prometheus_retries,
            )
        except Exception as exc:
            sys.exit(f"Prometheus preflight failed: {exc}")
    args.jaeger_services = []
    args.jaeger_planned_operations = []
    if args.collect_jaeger:
        try:
            from jaeger_collector import list_services
            discovery_prefix = "" if args.jaeger_metrics else args.jaeger_service_prefix
            discovered_services = list_services(
                args.jaeger_url, discovery_prefix,
                args.jaeger_timeout, args.jaeger_retries,
            )
        except Exception as exc:
            sys.exit(f"Jaeger preflight failed: {exc}")
        if not discovered_services:
            sys.exit(
                "Jaeger preflight found no services matching the collection mode"
            )
        if args.jaeger_requested_services:
            missing_services = sorted(
                set(args.jaeger_requested_services) - set(discovered_services)
            )
            if missing_services:
                sys.exit(
                    "Jaeger preflight did not find requested services: "
                    + ", ".join(missing_services)
                )
            args.jaeger_services = args.jaeger_requested_services
        elif args.jaeger_metrics:
            from jaeger_collector import metric_collection_plan
            service_suffixes, _operations = metric_collection_plan(args.jaeger_metrics)
            args.jaeger_services = [
                service for service in discovered_services
                if any(service.endswith(suffix) for suffix in service_suffixes)
            ]
            if not args.jaeger_services:
                sys.exit(
                    "Jaeger preflight found no services required by --jaeger-metrics"
                )
        else:
            args.jaeger_services = discovered_services
        if args.jaeger_metrics:
            from jaeger_collector import metric_collection_plan
            _service_suffixes, metric_operations = metric_collection_plan(args.jaeger_metrics)
        else:
            metric_operations = set()
        args.jaeger_planned_operations = sorted(metric_operations)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    CAMPAIGN_LOG_PATH = args.output_dir / "campaign.log"
    if args.collect_jaeger:
        planned_operations = list(dict.fromkeys([
            *args.jaeger_operations, *args.jaeger_planned_operations,
        ]))
        log_event(
            "jaeger-plan",
            f"{len(args.jaeger_services)} serviços: {', '.join(args.jaeger_services)} | "
            f"{len(planned_operations)} operações: {', '.join(planned_operations)}",
        )
    (args.output_dir / "dataset_manifest.json").write_text(
        json.dumps(args.dataset_manifest, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "execution_provenance.json").write_text(
        json.dumps(args.execution_provenance, indent=2) + "\n", encoding="utf-8"
    )
    rng = random.Random(args.seed)
    cells = []
    for repetition in range(1, args.repetitions + 1):
        block = [(users, workload, repetition) for users in args.loads for workload in args.workloads]
        rng.shuffle(block)
        cells.extend(block)
    pending_cells = [
        cell for cell in cells
        if not (args.skip_existing and cell_is_complete(
            args,
            args.output_dir / f"users-{cell[0]}__{cell[1]}__rep-{cell[2]:02d}",
        ))
    ]
    campaign_seconds = sum(
        math.ceil(users / args.spawn_rate) + args.stabilization
        + args.measurement_duration + args.collection_buffer
        + (args.jaeger_flush_wait if args.collect_jaeger else 0)
        for users, _workload, _repetition in pending_cells
    ) + max(0, len(pending_cells) - 1) * args.cooldown
    args.campaign_started = time.time()
    args.campaign_base_estimated_seconds = campaign_seconds
    args.campaign_estimated_seconds = campaign_seconds
    args.campaign_cell_total = len(pending_cells)
    args.campaign_cell_overheads = []
    args.campaign_current_cell_started = None
    args.campaign_current_cell_base_seconds = 0.0
    log_event(
        "campaign",
        f"{len(pending_cells)} células pendentes de {len(cells)} | "
        f"duração estimada {format_duration(campaign_seconds)} | "
        f"cooldown {format_duration(args.cooldown)} | ordem randomizada com seed {args.seed}",
    )
    paths = []
    pending_index = 0
    for cell in cells:
        is_pending = cell in pending_cells
        if is_pending:
            args.campaign_cell_index = pending_index + 1
        paths.append(run_cell(args, *cell))
        if is_pending:
            pending_index += 1
        if args.cooldown and is_pending and pending_index < len(pending_cells):
            cooldown_with_progress(args)
    log_event("combine", f"reunindo {len(paths)} arquivos de amostras")
    collection_status = write_collection_status(
        paths, args.output_dir / "collection_status.csv"
    )
    partial = [row for row in collection_status if row["cell_status"] != "complete"]
    if partial:
        log_event(
            "warning",
            f"{len(partial)} célula(s) parcial(is); consulte collection_status.csv. "
            "As análises disponíveis são descritivas até completar as coletas.",
        )
    combine(paths, args.output_dir / "all_samples.csv")
    interarrival_summaries = sorted(
        path.parent / "message_interarrival_summary.csv" for path in paths
        if (path.parent / "message_interarrival_summary.csv").is_file()
    )
    if interarrival_summaries:
        combine(
            interarrival_summaries,
            args.output_dir / "all_message_interarrival_summaries.csv",
        )
    interarrival_observations = sorted(
        path.parent / "message_interarrival_observations.csv" for path in paths
        if (path.parent / "message_interarrival_observations.csv").is_file()
    )
    if interarrival_observations:
        combine(
            interarrival_observations,
            args.output_dir / "all_message_interarrival_observations.csv",
        )
    request_interarrival_summaries = sorted(
        path.parent / "request_interarrival_summary.csv" for path in paths
        if (path.parent / "request_interarrival_summary.csv").is_file()
    )
    if request_interarrival_summaries:
        combine(request_interarrival_summaries,
                args.output_dir / "all_request_interarrival_summaries.csv")
    request_interarrival_observations = sorted(
        path.parent / "request_interarrival_observations.csv" for path in paths
        if (path.parent / "request_interarrival_observations.csv").is_file()
    )
    if request_interarrival_observations:
        combine(request_interarrival_observations,
                args.output_dir / "all_request_interarrival_observations.csv")
    jaeger_summaries = sorted(
        path.parent / "jaeger" / "repetition_summary.csv" for path in paths
        if (path.parent / "jaeger" / "repetition_summary.csv").is_file()
    )
    combined_jaeger = args.output_dir / "all_jaeger_repetition_summaries.csv"
    if jaeger_summaries:
        log_event("combine", f"reunindo {len(jaeger_summaries)} resumos do Jaeger")
        combine(jaeger_summaries, combined_jaeger)
    jaeger_samples = sorted(
        path.parent / "jaeger" / "samples.csv" for path in paths
        if (path.parent / "jaeger" / "samples.csv").is_file()
    )
    combined_jaeger_samples = args.output_dir / "all_jaeger_samples.csv"
    if jaeger_samples:
        combine(jaeger_samples, combined_jaeger_samples)
    derived_summaries = sorted(
        path.parent / "jaeger" / "derived_repetition_summary.csv" for path in paths
        if (path.parent / "jaeger" / "derived_repetition_summary.csv").is_file()
    )
    combined_derived = args.output_dir / "all_jaeger_derived_repetition_summaries.csv"
    if derived_summaries:
        combine(derived_summaries, combined_derived)
    derived_samples = sorted(
        path.parent / "jaeger" / "derived_samples.csv" for path in paths
        if (path.parent / "jaeger" / "derived_samples.csv").is_file()
    )
    combined_derived_samples = args.output_dir / "all_jaeger_derived_samples.csv"
    if derived_samples:
        combine(derived_samples, combined_derived_samples)
    quality_paths = sorted(
        path.parent / "jaeger" / "data_quality.csv" for path in paths
        if (path.parent / "jaeger" / "data_quality.csv").is_file()
    )
    combined_quality = args.output_dir / "analysis" / "data_quality.csv"
    if quality_paths:
        combined_quality.parent.mkdir(parents=True, exist_ok=True)
        log_event("quality", f"reunindo {len(quality_paths)} relatórios de qualidade")
        combine(quality_paths, combined_quality)
    if not args.skip_analysis:
        log_event("analysis", "gerando resumos, IC 95%, gráficos, ANOVA e Tukey HSD")
        from analyze_results import generate_analysis
        generate_analysis(args.output_dir / "all_samples.csv", args.output_dir / "analysis")
        if jaeger_summaries:
            from analyze_results import generate_jaeger_analysis
            generate_jaeger_analysis(
                combined_jaeger, args.output_dir / "analysis" / "jaeger",
                combined_jaeger_samples if jaeger_samples else None,
            )
        if derived_summaries:
            from analyze_results import generate_jaeger_analysis
            generate_jaeger_analysis(
                combined_derived, args.output_dir / "analysis" / "jaeger-derived",
                combined_derived_samples if derived_samples else None,
            )
    if quality_paths:
        write_campaign_report(args.output_dir, combined_quality, len(paths))
        log_event("quality", f"relatório consolidado em {args.output_dir / 'EXPERIMENT_REPORT.md'}")
    elapsed = time.time() - args.campaign_started
    log_event(
        "done",
        f"campanha concluída em {format_duration(elapsed)}: {args.output_dir / 'all_samples.csv'}",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log_event("interrupt", "campanha interrompida pelo usuário")
        raise SystemExit(130) from None
