#!/usr/bin/env python3
"""Run the 3 x 2 Matrix load-test design and build 31-sample datasets."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
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


LOADS = (50, 100, 150)
WORKLOADS = ("text_only", "text_and_image")
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


def prometheus_query_range(base_url: str, query: str, start: float, end: float, step: float) -> list[dict]:
    params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    url = f"{base_url.rstrip('/')}/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.load(response)
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


def write_samples(path: Path, locust_rows: list[dict], locust_history: list[dict],
                  cpu: dict[float, dict[str, float]],
                  synapse_metrics: dict[str, dict[float, float]], metadata: dict) -> None:
    jobs = sorted({job for values in cpu.values() for job in values})
    include_prometheus = bool(cpu) or bool(synapse_metrics)
    fields = [
        "sample", "timestamp", "users", "workload", "repetition", "rps",
        "failures_per_second", "avg_response_time_ms", "median_response_time_ms",
        "p95_response_time_ms", "foreground_rps", "foreground_failures_per_second",
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
            }
            row.update(locust_breakdown(locust_history, timestamp))
            if include_prometheus:
                row["cpu_total_percent"] = sum(cpu_values.values())
                row.update({f"cpu_{job}_percent": cpu_values.get(job, "") for job in jobs})
                row.update({name: nearest_scalar(synapse_metrics.get(name, {}), timestamp)
                            for name in SYNAPSE_METRICS})
            writer.writerow(row)


def run_cell(args: argparse.Namespace, users: int, workload: str, repetition: int) -> Path:
    name = f"users-{users}__{workload}__rep-{repetition:02d}"
    cell_dir = args.output_dir / name
    samples_path = cell_dir / "samples.csv"
    if args.skip_existing and samples_path.exists():
        log_event("skip", f"célula já concluída: {name}")
        return samples_path
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
    expected_ramp_seconds = math.ceil(users / args.spawn_rate)
    run_time_seconds = (
        expected_ramp_seconds + args.stabilization
        + args.measurement_duration + args.collection_buffer
    )
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
        "run_time_seconds": run_time_seconds, "samples": args.samples,
        "started_epoch": started, "command": command,
        "dataset": args.dataset_manifest,
    }
    (cell_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    log_event(
        "cell",
        f"iniciando {args.campaign_cell_index}/{args.campaign_cell_total}: {name} | "
        f"duração estimada {format_duration(run_time_seconds)} | {campaign_progress(args)}",
    )
    with (cell_dir / "locust.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return_code = wait_for_locust(process, args, name, started, expected_ramp_seconds)
        except KeyboardInterrupt:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait()
            raise
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
            args.prometheus_url, query, measure_start, measure_end, step
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
                args.prometheus_url, metric_query, measure_start, measure_end, step
            )
            synapse_metrics[metric_name] = scalar_by_timestamp(series)
            if not synapse_metrics[metric_name]:
                log_event("warning", f"Prometheus não retornou valores para {metric_name}")
    else:
        log_event("collect", f"{name}: coleta do Prometheus desabilitada")
    write_samples(
        samples_path, history, all_history, cpu_by_timestamp(cpu_series), synapse_metrics, metadata
    )
    metadata.update({
        "ended_epoch": ended,
        "full_load_epoch": full_load_at,
        "actual_ramp_seconds": full_load_at - min(float(row["Timestamp"]) for row in aggregate_history),
        "measurement_start_epoch": measure_start,
        "measurement_end_epoch": measure_end,
        "promql": query,
        "observed_prometheus_jobs": observed_jobs,
        "synapse_metric_queries": metric_queries,
    })
    (cell_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
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
    # Fail before a long campaign only when remote metrics were requested.
    if args.collect_prometheus:
        try:
            prometheus_query_range(args.prometheus_url, "up", time.time() - 10, time.time(), 10)
        except Exception as exc:
            sys.exit(f"Prometheus preflight failed: {exc}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    CAMPAIGN_LOG_PATH = args.output_dir / "campaign.log"
    (args.output_dir / "dataset_manifest.json").write_text(
        json.dumps(args.dataset_manifest, indent=2) + "\n", encoding="utf-8"
    )
    rng = random.Random(args.seed)
    cells = []
    for repetition in range(1, args.repetitions + 1):
        block = [(users, workload, repetition) for users in args.loads for workload in args.workloads]
        rng.shuffle(block)
        cells.extend(block)
    pending_cells = [
        cell for cell in cells
        if not (args.skip_existing and (
            args.output_dir
            / f"users-{cell[0]}__{cell[1]}__rep-{cell[2]:02d}"
            / "samples.csv"
        ).exists())
    ]
    campaign_seconds = sum(
        math.ceil(users / args.spawn_rate) + args.stabilization
        + args.measurement_duration + args.collection_buffer
        for users, _workload, _repetition in pending_cells
    ) + max(0, len(pending_cells) - 1) * args.cooldown
    args.campaign_started = time.time()
    args.campaign_estimated_seconds = campaign_seconds
    args.campaign_cell_total = len(pending_cells)
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
    combine(paths, args.output_dir / "all_samples.csv")
    if not args.skip_analysis:
        log_event("analysis", "gerando resumos, IC 95%, gráficos, ANOVA e Tukey HSD")
        from analyze_results import generate_analysis
        generate_analysis(args.output_dir / "all_samples.csv", args.output_dir / "analysis")
    elapsed = time.time() - args.campaign_started
    log_event(
        "done",
        f"campanha concluída em {format_duration(elapsed)}: {args.output_dir / 'all_samples.csv'}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
