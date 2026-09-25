from argparse import Namespace
import csv

from experiments import run_factorial


def test_campaign_progress_projects_observed_jaeger_overhead(monkeypatch):
    args = Namespace(
        campaign_started=1000.0,
        campaign_base_estimated_seconds=1500.0,
        campaign_estimated_seconds=1500.0,
        campaign_cell_total=5,
        campaign_cell_overheads=[180.0],
        campaign_current_cell_started=None,
        campaign_current_cell_base_seconds=260.0,
    )
    monkeypatch.setattr(run_factorial.time, "time", lambda: 1100.0)

    progress = run_factorial.campaign_progress(args)

    assert args.campaign_estimated_seconds == 2400.0
    assert "restante estimado 38m 20s" in progress


def test_campaign_progress_uses_active_collection_as_lower_bound(monkeypatch):
    args = Namespace(
        campaign_started=1000.0,
        campaign_base_estimated_seconds=500.0,
        campaign_estimated_seconds=500.0,
        campaign_cell_total=2,
        campaign_cell_overheads=[],
        campaign_current_cell_started=1000.0,
        campaign_current_cell_base_seconds=100.0,
    )
    monkeypatch.setattr(run_factorial.time, "time", lambda: 1150.0)

    run_factorial.campaign_progress(args)

    assert args.campaign_estimated_seconds == 600.0


def test_write_campaign_report_exposes_failed_metrics(tmp_path):
    quality = tmp_path / "analysis" / "data_quality.csv"
    quality.parent.mkdir()
    fields = [
        "users", "workload", "repetition", "metric", "valid_observations",
        "invalid_observations", "invalid_percent", "coverage_percent",
        "mean_ms", "p95_ms", "quality", "reason",
    ]
    with quality.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "users": 50, "workload": "text_only", "repetition": 1,
            "metric": "replication_dispatch_latency", "valid_observations": 6,
            "invalid_observations": 4, "invalid_percent": 40,
            "coverage_percent": "", "mean_ms": 1.2, "p95_ms": 2.0,
            "quality": "FAIL", "reason": "clock skew",
        })

    run_factorial.write_campaign_report(tmp_path, quality, 1)

    report = (tmp_path / "EXPERIMENT_REPORT.md").read_text(encoding="utf-8")
    assert "Overall data quality: **FAIL**" in report
    assert "replication_dispatch_latency" in report
    assert "clock skew" in report


def test_process_message_arrivals_uses_monotonic_clock_and_window(tmp_path):
    arrivals = tmp_path / "message_arrivals.csv"
    fields = ["sequence", "epoch_ns", "monotonic_ns", "message_type", "user", "endpoint"]
    rows = [
        # Outside the measured window: it must not become the previous T1 event.
        {"sequence": 1, "epoch_ns": 99_000_000_000, "monotonic_ns": 1_000_000_000,
         "message_type": "m.text", "user": "a", "endpoint": "send/m.text"},
        {"sequence": 2, "epoch_ns": 101_000_000_000, "monotonic_ns": 2_000_000_000,
         "message_type": "m.text", "user": "a", "endpoint": "send/m.text"},
        {"sequence": 3, "epoch_ns": 102_000_000_000, "monotonic_ns": 2_250_000_000,
         "message_type": "m.text", "user": "b", "endpoint": "send/m.text"},
        {"sequence": 4, "epoch_ns": 104_000_000_000, "monotonic_ns": 3_000_000_000,
         "message_type": "m.image", "user": "c", "endpoint": "send/m.image"},
    ]
    with arrivals.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    samples, summary = run_factorial.process_message_arrivals(
        arrivals, 100, 105, 6,
        {"users": 50, "workload": "text_and_image", "repetition": 1}, tmp_path,
    )

    assert summary["count"] == 2
    assert summary["mean_ms"] == 500.0
    assert samples == {3: 250.0, 5: 750.0}
    assert (tmp_path / "message_interarrival_observations.csv").is_file()
    assert (tmp_path / "message_interarrival_summary.csv").is_file()


def test_process_request_arrivals_groups_sync_and_foreground(tmp_path):
    arrivals = tmp_path / "request_arrivals.csv"
    fields = ["sequence", "epoch_ns", "monotonic_ns", "method", "endpoint", "user"]
    rows = [
        {"sequence": 1, "epoch_ns": 101_000_000_000, "monotonic_ns": 1_000_000_000,
         "method": "GET", "endpoint": "/_matrix/client/v3/sync", "user": "a"},
        {"sequence": 2, "epoch_ns": 102_000_000_000, "monotonic_ns": 1_100_000_000,
         "method": "PUT", "endpoint": "/_matrix/client/v3/rooms/_/send/m.text", "user": "a"},
        {"sequence": 3, "epoch_ns": 103_000_000_000, "monotonic_ns": 1_300_000_000,
         "method": "GET", "endpoint": "/_matrix/client/v3/sync", "user": "b"},
        {"sequence": 4, "epoch_ns": 104_000_000_000, "monotonic_ns": 1_600_000_000,
         "method": "PUT", "endpoint": "/_matrix/client/v3/rooms/_/send/m.text", "user": "b"},
    ]
    with arrivals.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    samples, summaries = run_factorial.process_request_arrivals(
        arrivals, 100, 105, 6,
        {"users": 50, "workload": "text_only", "repetition": 1}, tmp_path,
    )

    by_scope = {row["scope"]: row for row in summaries}
    assert by_scope["all"]["count"] == 3
    assert by_scope["sync"]["mean_ms"] == 300.0
    assert by_scope["foreground"]["mean_ms"] == 500.0
    assert by_scope["text_send"]["mean_ms"] == 500.0
    assert "sync" in samples and "foreground" in samples
    assert (tmp_path / "request_interarrival_observations.csv").is_file()
    assert (tmp_path / "request_interarrival_summary.csv").is_file()


def test_prometheus_query_retries_transient_failure(monkeypatch):
    attempts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"status":"success","data":{"result":[]}}'

    def urlopen(_url, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise TimeoutError("temporary")
        return Response()

    monkeypatch.setattr(run_factorial.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(run_factorial.time, "sleep", lambda _seconds: None)

    assert run_factorial.prometheus_query_range(
        "http://prometheus", "up", 1, 2, 1, timeout=7, retries=1
    ) == []
    assert attempts == [7, 7]
