from experiments import jaeger_collector
import pytest

from experiments.analyze_results import generate_jaeger_analysis
import pandas as pd


def test_collect_spans_filters_exact_window_and_deduplicates(monkeypatch):
    trace = {
        "traceID": "trace-1",
        "processes": {
            "p1": {
                "serviceName": "matrix.example master",
                "tags": [{"key": "hostname", "value": "worker-1"}],
            }
        },
        "spans": [
            {
                "traceID": "trace-1", "spanID": "before", "processID": "p1",
                "operationName": "db.query", "startTime": 99_000_000,
                "duration": 1000, "tags": [], "references": [],
            },
            {
                "traceID": "trace-1", "spanID": "inside", "processID": "p1",
                "operationName": "db.query", "startTime": 104_000_000,
                "duration": 2500,
                "tags": [{"key": "db.statement", "value": "SELECT 1"}],
                "references": [],
            },
        ],
    }

    monkeypatch.setattr(
        jaeger_collector, "list_operations",
        lambda *_args, **_kwargs: ["db.query"],
    )
    monkeypatch.setattr(
        jaeger_collector, "jaeger_get",
        lambda *_args, **_kwargs: {"data": [trace]},
    )

    spans, metadata, traces = jaeger_collector.collect_spans(
        "http://jaeger", ["matrix.example master"], ["db.query"],
        100, 110, 6, 5, 2, 1000, 1, 2, 1, 0, False, lambda _message: None,
    )

    assert len(spans) == 1
    assert spans[0]["span_id"] == "inside"
    assert spans[0]["sample"] == 3
    assert spans[0]["duration_ms"] == 2.5
    assert spans[0]["db_statement"] == ""
    assert metadata["unique_spans"] == 1
    assert len(traces) == 1


def test_saturated_query_is_split_automatically(monkeypatch):
    def fake_get(_base_url, _endpoint, params, _timeout, _retries):
        duration = (params["end"] - params["start"]) / 1_000_000
        return {"data": [{"traceID": str(index), "spans": [], "processes": {}}
                         for index in range(2 if duration > 1 else 1)]}

    monkeypatch.setattr(jaeger_collector, "jaeger_get", fake_get)
    traces, queries, unresolved, splits = jaeger_collector._query_trace_range(
        "http://jaeger", "service", "db.query", 0, 2, 2, 1, 1, 0
    )

    assert len(traces) == 2
    assert queries == 3
    assert splits == 1
    assert unresolved == []


def test_aggregate_spans_uses_run_as_statistical_unit():
    spans = [
        {"sample": 1, "service": "master", "operation": "db.query", "duration_ms": 1.0},
        {"sample": 1, "service": "master", "operation": "db.query", "duration_ms": 3.0},
        {"sample": 2, "service": "master", "operation": "db.query", "duration_ms": 5.0},
    ]
    experiment = {"users": 50, "workload": "text_only", "repetition": 2}

    samples, summary = jaeger_collector.aggregate_spans(
        spans, experiment, ["first", "second"]
    )

    all_samples = [row for row in samples if row["scope"] == "all"]
    all_summary = [row for row in summary if row["scope"] == "all"]
    assert len(all_samples) == 2
    assert all_samples[0]["mean_ms"] == 2.0
    assert len(all_summary) == 1
    assert all_summary[0]["count"] == 3
    assert all_summary[0]["mean_ms"] == 3.0
    assert all_summary[0]["repetition"] == 2


def test_nginx_metrics_and_foreground_scope():
    trace = {
        "traceID": "a" * 32,
        "processes": {
            "nginx": {"serviceName": "matrix-nginx"},
            "generic": {"serviceName": "matrix.example generic"},
        },
        "spans": [
            {
                "traceID": "a" * 32, "spanID": "nginx", "processID": "nginx",
                "operationName": "matrix-client-proxy", "startTime": 101_000_000,
                "duration": 50_000, "references": [],
                "tags": [
                    {"key": "http.target", "value": "/_matrix/client/v3/rooms/x/send/m.room.message/y"},
                    {"key": "http.status_code", "value": 200},
                    {"key": "nginx.request_time", "value": "0.050"},
                    {"key": "nginx.upstream_connect_time", "value": "0.002"},
                    {"key": "nginx.upstream_header_time", "value": "0.032"},
                    {"key": "nginx.upstream_response_time", "value": "0.040"},
                ],
            },
            {
                "traceID": "a" * 32, "spanID": "generic", "processID": "generic",
                "operationName": "RoomSendEventRestServlet", "startTime": 101_010_000,
                "duration": 20_000, "references": [], "tags": [],
            },
        ],
    }
    metrics = [name for name in jaeger_collector.DERIVED_METRIC_SPECS if name.startswith("nginx_")]
    experiment = {"users": 50, "workload": "text_only", "repetition": 1}
    observations = jaeger_collector.derive_metrics(
        [trace], metrics, 100, 110, 6, experiment
    )
    values = {row["metric"]: row for row in observations}

    assert values["nginx_total_request_latency"]["value_ms"] == 50.0
    assert values["nginx_worker_connection_latency"]["value_ms"] == 2.0
    assert values["nginx_upstream_first_byte_latency"]["value_ms"] == 32.0
    assert values["nginx_upstream_processing_latency"]["value_ms"] == 30.0
    assert values["nginx_upstream_response_transfer_latency"]["value_ms"] == 8.0
    assert values["nginx_proxy_client_overhead"]["value_ms"] == pytest.approx(10.0)
    assert values["nginx_proxy_overhead"]["value_ms"] == pytest.approx(10.0)
    assert values["nginx_proxy_overhead"]["raw_value_ms"] == pytest.approx(10.0)
    samples, _summary = jaeger_collector.aggregate_derived(
        observations, experiment, [str(index) for index in range(6)]
    )
    assert {row["scope"] for row in samples} == {"all", "message_send", "foreground"}


def test_general_nginx_metrics_include_sync():
    trace = {
        "traceID": "c" * 32,
        "processes": {"nginx": {"serviceName": "matrix-nginx"}},
        "spans": [{
            "traceID": "c" * 32, "spanID": "nginx", "processID": "nginx",
            "operationName": "matrix-client-proxy", "startTime": 101_000_000,
            "duration": 30_000, "references": [],
            "tags": [
                {"key": "http.target", "value": "/_matrix/client/v3/sync?timeout=30000"},
                {"key": "http.status_code", "value": 200},
                {"key": "nginx.request_time", "value": "0.030"},
                {"key": "nginx.upstream_connect_time", "value": "0.001"},
                {"key": "nginx.upstream_header_time", "value": "0.020"},
                {"key": "nginx.upstream_response_time", "value": "0.025"},
            ],
        }],
    }
    experiment = {"users": 50, "workload": "text_only", "repetition": 1}
    observations = jaeger_collector.derive_metrics(
        [trace], list(jaeger_collector.DERIVED_METRIC_SPECS),
        100, 110, 6, experiment,
    )

    assert {row["metric"] for row in observations} == {
        "nginx_total_request_latency",
        "nginx_worker_connection_latency",
        "nginx_upstream_first_byte_latency",
        "nginx_upstream_processing_latency",
        "nginx_upstream_response_transfer_latency",
        "nginx_proxy_client_overhead",
        "nginx_proxy_overhead",
    }
    assert all(row["endpoint"] == "sync" and row["valid"] for row in observations)
    samples, _summary = jaeger_collector.aggregate_derived(
        observations, experiment, [str(index) for index in range(6)]
    )
    assert {row["scope"] for row in samples} == {"all", "sync"}


def test_nginx_proxy_overhead_clamps_only_rounding_noise():
    def trace(trace_id, request_time, upstream_time):
        return {
            "traceID": trace_id,
            "processes": {"nginx": {"serviceName": "matrix-nginx"}},
            "spans": [{
                "traceID": trace_id, "spanID": trace_id, "processID": "nginx",
                "operationName": "matrix-client-proxy", "startTime": 101_000_000,
                "duration": 30_000, "references": [],
                "tags": [
                    {"key": "http.target", "value": "/_matrix/client/v3/sync"},
                    {"key": "http.status_code", "value": 200},
                    {"key": "nginx.request_time", "value": request_time},
                    {"key": "nginx.upstream_response_time", "value": upstream_time},
                ],
            }],
        }

    experiment = {"users": 10, "workload": "text_only", "repetition": 1}
    rows = jaeger_collector.derive_metrics(
        [trace("noise", "0.120", "0.121"), trace("bad", "0.120", "0.125")],
        ["nginx_proxy_overhead"], 100, 110, 6, experiment,
    )
    by_trace = {row["trace_id"]: row for row in rows}
    assert by_trace["noise"]["valid"] is True
    assert by_trace["noise"]["value_ms"] == 0.0
    assert by_trace["noise"]["raw_value_ms"] == pytest.approx(-1.0)
    assert by_trace["bad"]["valid"] is False
    assert by_trace["bad"]["raw_value_ms"] == pytest.approx(-5.0)


def test_derive_t4_to_t7_with_causal_validation():
    outer_id = "outer"
    trace = {
        "traceID": "trace",
        "processes": {},
        "spans": [
            {"spanID": "root", "operationName": "RoomSendEventRestServlet",
             "startTime": 100_500_000, "duration": 4_000_000, "references": []},
            {"spanID": "auth", "operationName": "get_user_by_req",
             "startTime": 101_000_000, "duration": 2_000,
             "references": [{"refType": "CHILD_OF", "spanID": "root"}]},
            {"spanID": "session", "operationName": "db.get_user_by_access_token",
             "startTime": 101_000_100, "duration": 1_500, "references": []},
            {"spanID": "wrapper", "operationName": "outgoing_replication_request",
             "startTime": 101_010_000, "duration": 20_000, "references": []},
            {"spanID": "client", "operationName": "outgoing-replication-request",
             "startTime": 102_000_000, "duration": 10_000,
             "references": [{"refType": "CHILD_OF", "spanID": outer_id}],
             "tags": [{"key": "span.kind", "value": "client"}]},
            {"spanID": "server", "operationName": "ReplicationSendEventsRestServlet",
             "startTime": 102_004_000, "duration": 5_000,
             "references": [{"refType": "CHILD_OF", "spanID": outer_id}]},
            {"spanID": "batch", "operationName": "persist_event_batch",
             "startTime": 103_000_000, "duration": 20_000, "references": []},
            {"spanID": "txn", "operationName": "db.txn",
             "startTime": 103_005_000, "duration": 7_000,
             "references": [], "tags": [{"key": "db.txn_desc", "value": "persist_events"}]},
        ],
    }
    experiment = {"users": 50, "workload": "text_only", "repetition": 1}

    rows = jaeger_collector.derive_metrics(
        [trace], list(jaeger_collector.DERIVED_METRIC_SPECS), 100, 110, 6, experiment
    )
    values = {row["metric"]: row for row in rows}

    assert values["request_authentication_latency"]["value_ms"] == 2.0
    assert values["replication_dispatch_latency"]["value_ms"] == 4.0
    assert values["replication_dispatch_latency"]["valid"] is True
    assert values["event_persister_pre_transaction_latency"]["value_ms"] == 5.0
    assert values["event_persistence_transaction_latency"]["value_ms"] == 7.0
    assert values["generic_worker_processing_latency"]["value_ms"] == 8.0
    assert values["postgresql_session_verification_latency"]["value_ms"] == 1.5


def test_metric_plan_selects_only_required_services_and_operations():
    services, operations = jaeger_collector.metric_collection_plan([
        "request_authentication_latency",
        "event_persistence_transaction_latency",
    ])
    assert services == {" generic", " events_persister"}
    assert operations == {"get_user_by_req", "persist_event_batch"}


def test_nginx_metric_plan_selects_only_local_proxy_operation():
    services, operations = jaeger_collector.metric_collection_plan([
        "nginx_total_request_latency",
        "nginx_proxy_overhead",
    ])
    assert services == {"matrix-nginx"}
    assert operations == {"matrix-client-proxy"}


def test_metric_formulas_returns_only_requested_metadata():
    requested = [
        "request_authentication_latency",
        "event_persistence_transaction_latency",
    ]

    formulas = jaeger_collector.metric_formulas(requested)

    assert list(formulas) == requested
    assert formulas["request_authentication_latency"] == "duration(get_user_by_req)"
    assert formulas["event_persistence_transaction_latency"] == (
        "duration(db.txn[db.txn_desc=persist_events])"
    )


def test_repeated_trace_representations_are_merged():
    first = {"traceID": "trace", "processes": {"p1": {"serviceName": "one"}},
             "spans": [{"traceID": "trace", "spanID": "a", "tags": []}]}
    second = {"traceID": "trace", "processes": {"p2": {"serviceName": "two"}},
              "spans": [{"traceID": "trace", "spanID": "b", "tags": []}]}

    merged = jaeger_collector._merge_trace(first, second)

    assert {span["spanID"] for span in merged["spans"]} == {"a", "b"}
    assert set(merged["processes"]) == {"p1", "p2"}


def test_t6_is_synthesized_as_run_level_difference():
    experiment = {"users": 50, "workload": "text_only", "repetition": 1}
    observations = [
        {**experiment, "metric": "_event_persister_servlet_latency",
         "endpoint": "message_send", "sample": 1, "value_ms": 30.0, "valid": True},
        {**experiment, "metric": "_event_persister_servlet_latency",
         "endpoint": "message_send", "sample": 1, "value_ms": 34.0, "valid": True},
        {**experiment, "metric": "event_persistence_transaction_latency",
         "endpoint": "other", "sample": 1, "value_ms": 10.0, "valid": True},
    ]

    samples, summaries = jaeger_collector.aggregate_derived(
        observations, experiment, ["timestamp"]
    )

    t6 = next(row for row in summaries
              if row["operation"] == "event_persister_processing_latency")
    assert t6["scope"] == "message_send"
    assert t6["mean_ms"] == 22.0
    assert not any(row["operation"] == "_event_persister_servlet_latency"
                   for row in samples + summaries)


def test_quality_report_flags_t5_coverage_and_dispatch_invalid_rows(tmp_path):
    experiment = {"users": 50, "workload": "text_only", "repetition": 1}
    observations = [
        {**experiment, "metric": "request_authentication_latency", "valid": True,
         "value_ms": 1.0, "trace_id": "a"},
        {**experiment, "metric": "request_authentication_latency", "valid": True,
         "value_ms": 1.0, "trace_id": "b"},
        {**experiment, "metric": "postgresql_session_verification_latency", "valid": True,
         "value_ms": 2.0, "trace_id": "a"},
        {**experiment, "metric": "replication_dispatch_latency", "valid": True,
         "value_ms": 3.0, "trace_id": "a"},
        {**experiment, "metric": "replication_dispatch_latency", "valid": False,
         "value_ms": -1.0, "trace_id": "b"},
    ]
    metadata = {
        **experiment,
        "derived_metrics": [
            "request_authentication_latency", "postgresql_session_verification_latency",
            "replication_dispatch_latency",
        ],
        "saturated_queries": [],
    }
    thresholds = {
        "warn_invalid_percent": 5.0, "fail_invalid_percent": 20.0,
        "warn_t4_coverage_percent": 90.0, "fail_t4_coverage_percent": 70.0,
        "warn_zero_percent": 80.0,
    }

    report = jaeger_collector.write_quality_artifacts(
        tmp_path, observations, [], metadata, thresholds,
    )

    by_metric = {row["metric"]: row for row in report["rows"]}
    assert report["overall"] == "FAIL"
    assert by_metric["postgresql_session_verification_latency"]["coverage_percent"] == 50.0
    assert by_metric["postgresql_session_verification_latency"]["quality"] == "FAIL"
    assert by_metric["replication_dispatch_latency"]["invalid_percent"] == 50.0
    assert (tmp_path / "DATA_QUALITY.md").is_file()


def test_jaeger_analysis_uses_repetition_summaries(tmp_path):
    rows = []
    for users in (50, 100):
        for workload_index, workload in enumerate(("text_only", "text_and_image")):
            for repetition in (1, 2):
                value = users / 10 + workload_index + repetition / 10
                rows.append({
                    "users": users, "workload": workload, "repetition": repetition,
                    "service": "matrix.example master", "operation": "db.query",
                    "count": 10, "mean_ms": value, "median_ms": value,
                    "p95_ms": value + 1, "p99_ms": value + 2, "max_ms": value + 3,
                })
    source = tmp_path / "jaeger.csv"
    output = tmp_path / "analysis"
    pd.DataFrame(rows).to_csv(source, index=False)

    generate_jaeger_analysis(source, output)

    assert (output / "confidence_intervals.csv").is_file()
    assert (output / "all_metrics_by_scenario.csv").is_file()
    assert (output / "plots" / "all_metrics_mean_ms.png").is_file()
    assert (output / "plots" / "all_metrics_p95_ms.png").is_file()
    assert (output / "plots" / "all_metrics_normalized_mean.png").is_file()
    assert list((output / "anova").glob("anova_*__p95_ms.csv"))
    assert list((output / "tukey").glob("tukey_*__p95_ms.csv"))
    assert list((output / "plots").glob("*__p95_ms.png"))
