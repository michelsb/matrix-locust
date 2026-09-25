"""Collect and aggregate Jaeger spans for one experimental window."""

from __future__ import annotations

import csv
import json
import math
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median


SPAN_FIELDS = (
    "sample", "timestamp", "trace_id", "span_id", "parent_span_id", "service",
    "operation", "endpoint", "duration_ms", "hostname", "instance_name",
    "request_id", "http_method", "http_target", "http_status_code",
    "worker_upstream", "upstream_addr", "upstream_connect_time",
    "upstream_header_time", "upstream_response_time", "request_time",
    "request_length", "bytes_sent", "connection", "connection_requests",
    "db_type", "db_statement",
)
AGGREGATE_FIELDS = (
    "users", "workload", "repetition", "sample", "timestamp", "scope",
    "service", "operation", "count", "mean_ms", "median_ms", "p95_ms",
    "p99_ms", "max_ms",
)
DERIVED_FIELDS = (
    "users", "workload", "repetition", "metric", "endpoint", "sample",
    "timestamp", "value_ms", "raw_value_ms", "valid", "reason", "trace_id", "primary_span_id",
    "secondary_span_id", "primary_operation", "secondary_operation",
)
QUALITY_FIELDS = (
    "users", "workload", "repetition", "metric", "valid_observations",
    "invalid_observations", "invalid_percent", "coverage_percent", "mean_ms",
    "p95_ms", "quality", "reason",
)
DERIVED_METRIC_SPECS = {
    "request_authentication_latency": {
        "service_suffixes": (" generic",), "anchors": ("get_user_by_req",),
        "formula": "duration(get_user_by_req)",
    },
    "replication_dispatch_latency": {
        "service_suffixes": (" generic", " events_persister"),
        "anchors": ("ReplicationSendEventsRestServlet",),
        "formula": "start(ReplicationSendEventsRestServlet) - start(outgoing-replication-request)",
    },
    "event_persister_pre_transaction_latency": {
        "service_suffixes": (" events_persister",), "anchors": ("persist_event_batch",),
        "formula": "start(db.txn[db.txn_desc=persist_events]) - start(persist_event_batch)",
    },
    "event_persistence_transaction_latency": {
        "service_suffixes": (" events_persister",), "anchors": ("persist_event_batch",),
        "formula": "duration(db.txn[db.txn_desc=persist_events])",
    },
    "event_persister_processing_latency": {
        "service_suffixes": (" events_persister",),
        "anchors": ("ReplicationSendEventsRestServlet", "persist_event_batch"),
        "formula": (
            "mean(duration(ReplicationSendEventsRestServlet), scope=message_send, run) "
            "- mean(event_persistence_transaction_latency, scope=other, run)"
        ),
    },
    "generic_worker_processing_latency": {
        "service_suffixes": (" generic",), "anchors": ("RoomSendEventRestServlet",),
        "formula": "start(outgoing_replication_request) - end(get_user_by_req)",
    },
    "postgresql_session_verification_latency": {
        "service_suffixes": (" generic",), "anchors": ("db.get_user_by_access_token",),
        "formula": "duration(db.get_user_by_access_token)",
    },
    "nginx_total_request_latency": {
        "service_suffixes": ("matrix-nginx",), "anchors": ("matrix-client-proxy",),
        "formula": "duration(matrix-client-proxy)",
    },
    "nginx_worker_connection_latency": {
        "service_suffixes": ("matrix-nginx",), "anchors": ("matrix-client-proxy",),
        "formula": "sum(nginx.upstream_connect_time) * 1000",
    },
    "nginx_upstream_first_byte_latency": {
        "service_suffixes": ("matrix-nginx",), "anchors": ("matrix-client-proxy",),
        "formula": "sum(nginx.upstream_header_time) * 1000",
    },
    "nginx_upstream_processing_latency": {
        "service_suffixes": ("matrix-nginx",), "anchors": ("matrix-client-proxy",),
        "formula": "(sum(nginx.upstream_header_time) - sum(nginx.upstream_connect_time)) * 1000",
    },
    "nginx_upstream_response_transfer_latency": {
        "service_suffixes": ("matrix-nginx",), "anchors": ("matrix-client-proxy",),
        "formula": "(sum(nginx.upstream_response_time) - sum(nginx.upstream_header_time)) * 1000",
    },
    "nginx_proxy_client_overhead": {
        "service_suffixes": ("matrix-nginx",), "anchors": ("matrix-client-proxy",),
        "formula": "(nginx.request_time - sum(nginx.upstream_response_time)) * 1000",
    },
    # Backward-compatible alias. Prefer nginx_proxy_client_overhead in new campaigns.
    "nginx_proxy_overhead": {
        "service_suffixes": ("matrix-nginx",), "anchors": ("matrix-client-proxy",),
        "formula": "(nginx.request_time - sum(nginx.upstream_response_time)) * 1000",
    },
}


FOREGROUND_ENDPOINTS = {"message_send", "media_upload"}


def endpoint_from_trace(trace: dict) -> str:
    """Classify a trace by its HTTP root without relying on dynamic IDs."""
    operations = {span.get("operationName", "") for span in trace.get("spans") or []}
    targets = []
    for span in trace.get("spans") or []:
        tags = _tag_map(span.get("tags"))
        target = str(tags.get("http.target", ""))
        if target:
            targets.append(target.split("?", 1)[0])
    if any("/_matrix/client/" in target and target.endswith("/sync") for target in targets) \
            or "SyncRestServlet" in operations:
        return "sync"
    if any("/send/m.room.message/" in target for target in targets) \
            or "RoomSendEventRestServlet" in operations:
        return "message_send"
    if any("/_matrix/media/" in target and target.endswith("/upload") for target in targets):
        return "media_upload"
    return "other"


def scopes_for_endpoint(endpoint: str) -> tuple[str, ...]:
    scopes = ["all", endpoint]
    if endpoint in FOREGROUND_ENDPOINTS:
        scopes.append("foreground")
    return tuple(dict.fromkeys(scopes))


def _nginx_time_values(value: object) -> list[float]:
    values = []
    for item in str(value or "").split(","):
        item = item.strip()
        if item and item != "-":
            try:
                values.append(float(item))
            except ValueError:
                continue
    return values


def metric_collection_plan(metrics: list[str]) -> tuple[set[str], set[str]]:
    services: set[str] = set()
    operations: set[str] = set()
    for metric in metrics:
        spec = DERIVED_METRIC_SPECS[metric]
        services.update(spec["service_suffixes"])
        operations.update(spec["anchors"])
    return services, operations


def metric_formulas(metrics: list[str]) -> dict[str, str]:
    """Return the canonical formulas persisted in collection metadata."""
    return {metric: DERIVED_METRIC_SPECS[metric]["formula"] for metric in metrics}


def _request_json(url: str, timeout: float, retries: int) -> dict:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return json.load(response)
        except Exception as exc:  # network and malformed responses are retried together
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 5))
    raise RuntimeError(f"Jaeger request failed after {retries + 1} attempts: {last_error}")


def jaeger_get(base_url: str, endpoint: str, params: dict | None,
               timeout: float, retries: int) -> dict:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    payload = _request_json(f"{base_url.rstrip('/')}{endpoint}{query}", timeout, retries)
    if payload.get("errors"):
        raise RuntimeError(f"Jaeger returned errors: {payload['errors']}")
    if "data" not in payload:
        raise RuntimeError("Jaeger response has no data field")
    return payload


def list_services(base_url: str, prefix: str, timeout: float, retries: int) -> list[str]:
    payload = jaeger_get(base_url, "/api/services", None, timeout, retries)
    return sorted(service for service in payload["data"] if service.startswith(prefix))


def list_operations(base_url: str, service: str, timeout: float, retries: int) -> list[str]:
    payload = jaeger_get(
        base_url, "/api/operations", {"service": service}, timeout, retries
    )
    return sorted(item["name"] if isinstance(item, dict) else item for item in payload["data"])


def percentile(values: list[float], probability: float) -> float:
    values = sorted(values)
    if not values:
        return float("nan")
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _tag_map(tags: list[dict] | None) -> dict[str, object]:
    return {tag.get("key", ""): tag.get("value", "") for tag in tags or []}


def _parent_id(span: dict) -> str:
    for reference in span.get("references") or []:
        if reference.get("refType") == "CHILD_OF":
            return str(reference.get("spanID", ""))
    return ""


def _merge_trace(target: dict | None, incoming: dict) -> dict:
    """Merge repeated Jaeger representations without discarding spans."""
    if target is None:
        return {
            **incoming,
            "spans": list(incoming.get("spans") or []),
            "processes": dict(incoming.get("processes") or {}),
        }
    processes = dict(target.get("processes") or {})
    processes.update(incoming.get("processes") or {})
    spans = {
        (str(span.get("traceID", incoming.get("traceID", ""))), str(span.get("spanID", ""))): span
        for span in target.get("spans") or []
    }
    for span in incoming.get("spans") or []:
        key = (str(span.get("traceID", incoming.get("traceID", ""))), str(span.get("spanID", "")))
        current = spans.get(key)
        # Prefer the representation containing more tags/references.
        if current is None or (
            len(span.get("tags") or []) + len(span.get("references") or [])
            > len(current.get("tags") or []) + len(current.get("references") or [])
        ):
            spans[key] = span
    return {**target, **incoming, "processes": processes, "spans": list(spans.values())}


def _sample_number(start_seconds: float, measure_start: float,
                   measure_end: float, samples: int) -> int:
    step = (measure_end - measure_start) / (samples - 1)
    return max(1, min(samples, round((start_seconds - measure_start) / step) + 1))


def _query_trace_range(base_url: str, service: str, operation: str,
                       start: float, end: float, limit: int, minimum_chunk: float,
                       timeout: float, retries: int) -> tuple[list[dict], int, list[dict], int]:
    """Query one range and bisect saturated responses until the minimum chunk."""
    payload = jaeger_get(base_url, "/api/traces", {
        "service": service, "operation": operation,
        "start": round(start * 1_000_000), "end": round(end * 1_000_000),
        "limit": limit,
    }, timeout, retries)
    traces = payload["data"]
    if len(traces) < limit:
        return traces, 1, [], 0
    if end - start > minimum_chunk:
        midpoint = start + (end - start) / 2
        left, left_count, left_saturated, left_splits = _query_trace_range(
            base_url, service, operation, start, midpoint, limit, minimum_chunk,
            timeout, retries,
        )
        right, right_count, right_saturated, right_splits = _query_trace_range(
            base_url, service, operation, midpoint, end, limit, minimum_chunk,
            timeout, retries,
        )
        return (
            left + right, 1 + left_count + right_count,
            left_saturated + right_saturated, 1 + left_splits + right_splits,
        )
    return traces, 1, [{
        "service": service, "operation": operation,
        "start": start, "end": end, "limit": limit,
    }], 0


def collect_spans(base_url: str, services: list[str], operations: list[str],
                  measure_start: float, measure_end: float, samples: int,
                  chunk_seconds: int, padding_seconds: int, limit: int,
                  minimum_chunk_seconds: float, workers: int,
                  timeout: float, retries: int, include_db_statement: bool,
                  warning, progress=None) -> tuple[list[dict], dict, list[dict]]:
    query_start = measure_start - padding_seconds
    query_end = measure_end + padding_seconds
    spans: dict[tuple[str, str], dict] = {}
    query_count = 0
    saturated_queries = []
    split_queries = 0
    raw_traces: dict[str, dict] = {}

    available_by_service = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(list_operations, base_url, service, timeout, retries): service
            for service in services
        }
        for completed, future in enumerate(as_completed(futures), 1):
            available_by_service[futures[future]] = set(future.result())
            if progress:
                progress(f"discovered operations for {completed}/{len(futures)} services")

    tasks = []
    for service in services:
        available = available_by_service[service]
        for operation in operations:
            if operation not in available:
                continue
            chunk_start = query_start
            while chunk_start < query_end:
                chunk_end = min(query_end, chunk_start + chunk_seconds)
                tasks.append((service, operation, chunk_start, chunk_end))
                chunk_start = chunk_end

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _query_trace_range, base_url, service, operation,
                chunk_start, chunk_end, limit, minimum_chunk_seconds,
                timeout, retries,
            ): (service, operation)
            for service, operation, chunk_start, chunk_end in tasks
        }
        for completed, future in enumerate(as_completed(futures), 1):
            traces, queries, unresolved, splits = future.result()
            query_count += queries
            split_queries += splits
            saturated_queries.extend(unresolved)
            service, operation = futures[future]
            if progress:
                progress(
                    f"completed {completed}/{len(futures)} ranges; "
                    f"{query_count} HTTP queries including automatic splits"
                )
            for trace in traces:
                trace_id = str(trace.get("traceID", ""))
                raw_traces[trace_id] = _merge_trace(raw_traces.get(trace_id), trace)
                processes = trace.get("processes") or {}
                endpoint = endpoint_from_trace(trace)
                for span in trace.get("spans") or []:
                    span_service = (processes.get(span.get("processID"), {})
                                    .get("serviceName", ""))
                    start_seconds = float(span.get("startTime", 0)) / 1_000_000
                    if (span_service != service
                            or span.get("operationName") != operation
                            or not measure_start <= start_seconds < measure_end):
                        continue
                    tags = _tag_map(span.get("tags"))
                    process_tags = _tag_map(
                        processes.get(span.get("processID"), {}).get("tags")
                    )
                    key = (str(span.get("traceID", "")), str(span.get("spanID", "")))
                    spans[key] = {
                        "sample": _sample_number(
                            start_seconds, measure_start, measure_end, samples
                        ),
                        "timestamp": datetime.fromtimestamp(
                            start_seconds, timezone.utc
                        ).isoformat(),
                        "trace_id": key[0], "span_id": key[1],
                        "parent_span_id": _parent_id(span),
                        "service": span_service, "operation": operation,
                        "endpoint": endpoint,
                        "duration_ms": float(span.get("duration", 0)) / 1000,
                        "hostname": process_tags.get("hostname", ""),
                        "instance_name": process_tags.get("instance_name", ""),
                        "request_id": tags.get("nginx.request_id", tags.get("request_id", "")),
                        "http_method": tags.get("http.method", ""),
                        "http_target": tags.get("http.target", ""),
                        "http_status_code": tags.get("http.status_code", ""),
                        "worker_upstream": tags.get("matrix.worker_upstream", ""),
                        "upstream_addr": tags.get("nginx.upstream_addr", ""),
                        "upstream_connect_time": tags.get("nginx.upstream_connect_time", ""),
                        "upstream_header_time": tags.get("nginx.upstream_header_time", ""),
                        "upstream_response_time": tags.get("nginx.upstream_response_time", ""),
                        "request_time": tags.get("nginx.request_time", ""),
                        "request_length": tags.get("nginx.request_length", ""),
                        "bytes_sent": tags.get("nginx.bytes_sent", ""),
                        "connection": tags.get("nginx.connection", ""),
                        "connection_requests": tags.get("nginx.connection_requests", ""),
                        "db_type": tags.get("db.type", ""),
                        "db_statement": tags.get("db.statement", "")
                        if include_db_statement else "",
                    }

    for saturated in saturated_queries:
        warning(
            f"Jaeger query still reached limit={limit} at minimum chunk: "
            f"{saturated['service']} / {saturated['operation']}"
        )

    metadata = {
        "query_start_epoch": query_start,
        "query_end_epoch": query_end,
        "measurement_start_epoch": measure_start,
        "measurement_end_epoch": measure_end,
        "queries": query_count,
        "automatic_splits": split_queries,
        "saturated_queries": saturated_queries,
        "unique_spans": len(spans),
    }
    return (
        sorted(spans.values(), key=lambda row: (row["timestamp"], row["span_id"])),
        metadata, list(raw_traces.values()),
    )


def derive_metrics(traces: list[dict], metrics: list[str], measure_start: float,
                   measure_end: float, samples: int, experiment: dict) -> list[dict]:
    """Calculate named internal-latency observations from trace spans."""
    requested = set(metrics)
    wants_estimated_t6 = "event_persister_processing_latency" in requested
    effective_requested = set(requested)
    if wants_estimated_t6:
        effective_requested.add("event_persistence_transaction_latency")
    observations = []

    def in_window(span: dict) -> bool:
        start = float(span.get("startTime", 0)) / 1_000_000
        return measure_start <= start < measure_end

    def add(metric: str, trace_id: str, primary: dict, secondary: dict | None,
            value_ms: float | None, valid: bool, reason: str = "",
            raw_value_ms: float | None = None) -> None:
        start = float(primary.get("startTime", 0)) / 1_000_000
        observations.append({
            **{key: experiment[key] for key in ("users", "workload", "repetition")},
            "metric": metric, "endpoint": endpoint,
            "sample": _sample_number(start, measure_start, measure_end, samples),
            "timestamp": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "value_ms": value_ms if value_ms is not None else "",
            "raw_value_ms": (
                raw_value_ms if raw_value_ms is not None else
                value_ms if value_ms is not None else ""
            ),
            "valid": valid, "reason": reason, "trace_id": trace_id,
            "primary_span_id": primary.get("spanID", ""),
            "secondary_span_id": secondary.get("spanID", "") if secondary else "",
            "primary_operation": primary.get("operationName", ""),
            "secondary_operation": secondary.get("operationName", "") if secondary else "",
        })

    for trace in traces:
        trace_id = str(trace.get("traceID", ""))
        spans = trace.get("spans") or []
        endpoint = endpoint_from_trace(trace)
        nginx_statuses = []
        processes = trace.get("processes") or {}
        for span in spans:
            if (span.get("operationName") == "matrix-client-proxy"
                    and processes.get(span.get("processID"), {}).get(
                        "serviceName", ""
                    ).endswith("matrix-nginx")):
                status = int(_tag_map(span.get("tags")).get("http.status_code", 0) or 0)
                nginx_statuses.append(status)
        trace_success = not nginx_statuses or any(200 <= status < 300 for status in nginx_statuses)
        trace_failure_reason = "" if trace_success else "trace HTTP response is not 2xx"
        if "request_authentication_latency" in requested:
            for span in spans:
                if span.get("operationName") == "get_user_by_req" and in_window(span):
                    add("request_authentication_latency", trace_id, span, None,
                        float(span.get("duration", 0)) / 1000, trace_success,
                        trace_failure_reason)

        if "replication_dispatch_latency" in requested:
            clients = [span for span in spans
                       if span.get("operationName") == "outgoing-replication-request"
                       and _tag_map(span.get("tags")).get("span.kind") == "client"]
            for server in spans:
                if (server.get("operationName") != "ReplicationSendEventsRestServlet"
                        or not in_window(server)):
                    continue
                server_parents = {
                    ref.get("spanID") for ref in server.get("references") or []
                    if ref.get("refType") == "CHILD_OF"
                }
                candidates = []
                for client in clients:
                    client_parents = {
                        ref.get("spanID") for ref in client.get("references") or []
                        if ref.get("refType") == "CHILD_OF"
                    }
                    if server_parents & client_parents:
                        candidates.append(client)
                if not candidates:
                    add("replication_dispatch_latency", trace_id, server, None, None,
                        False, "client span not found")
                    continue
                client = min(
                    candidates,
                    key=lambda span: abs(float(server["startTime"]) - float(span["startTime"])),
                )
                delta_us = float(server["startTime"]) - float(client["startTime"])
                client_duration = float(client.get("duration", 0))
                causally_valid = 0 <= delta_us <= client_duration
                add(
                    "replication_dispatch_latency", trace_id, server, client,
                    delta_us / 1000,
                    causally_valid and trace_success,
                    trace_failure_reason if causally_valid and not trace_success else (
                        "" if causally_valid else "clock skew violates client/server ordering"
                    ),
                )

        persistence_metrics = {
            "event_persister_pre_transaction_latency",
            "event_persistence_transaction_latency",
        }
        if effective_requested & persistence_metrics:
            transactions = [
                span for span in spans
                if span.get("operationName") == "db.txn"
                and _tag_map(span.get("tags")).get("db.txn_desc") == "persist_events"
            ]
            for batch in spans:
                if batch.get("operationName") != "persist_event_batch" or not in_window(batch):
                    continue
                batch_start = float(batch.get("startTime", 0))
                batch_end = batch_start + float(batch.get("duration", 0))
                candidates = [span for span in transactions
                              if batch_start <= float(span.get("startTime", 0)) <= batch_end]
                if not candidates:
                    for metric in sorted(effective_requested & persistence_metrics):
                        add(metric, trace_id, batch, None, None, False,
                            "persist_events transaction not found inside batch")
                    continue
                transaction = min(candidates, key=lambda span: float(span["startTime"]))
                if "event_persister_pre_transaction_latency" in requested:
                    add(
                        "event_persister_pre_transaction_latency", trace_id,
                        transaction, batch,
                        (float(transaction["startTime"]) - batch_start) / 1000,
                        trace_success, trace_failure_reason,
                    )
                if "event_persistence_transaction_latency" in effective_requested:
                    add(
                        "event_persistence_transaction_latency", trace_id,
                        transaction, batch,
                        float(transaction.get("duration", 0)) / 1000, trace_success,
                        trace_failure_reason,
                    )

        if wants_estimated_t6:
            for span in spans:
                if (span.get("operationName") == "ReplicationSendEventsRestServlet"
                        and in_window(span)):
                    add(
                        "_event_persister_servlet_latency", trace_id, span, None,
                        float(span.get("duration", 0)) / 1000,
                        trace_success, trace_failure_reason,
                    )

        if "generic_worker_processing_latency" in requested:
            authentications = [span for span in spans
                               if span.get("operationName") == "get_user_by_req"]
            outgoing = [span for span in spans
                        if span.get("operationName") == "outgoing_replication_request"]
            for root in spans:
                if root.get("operationName") != "RoomSendEventRestServlet" or not in_window(root):
                    continue
                root_id = root.get("spanID")
                auth_candidates = [span for span in authentications if any(
                    ref.get("refType") == "CHILD_OF" and ref.get("spanID") == root_id
                    for ref in span.get("references") or []
                )]
                root_start = float(root.get("startTime", 0))
                root_end = root_start + float(root.get("duration", 0))
                outgoing_candidates = [span for span in outgoing
                                       if root_start <= float(span.get("startTime", 0)) <= root_end]
                if not auth_candidates or not outgoing_candidates:
                    add("generic_worker_processing_latency", trace_id, root, None, None,
                        False, "authentication or outgoing replication span not found")
                    continue
                authentication = min(auth_candidates, key=lambda span: float(span["startTime"]))
                replication = min(outgoing_candidates, key=lambda span: float(span["startTime"]))
                auth_end = (float(authentication["startTime"])
                            + float(authentication.get("duration", 0)))
                delta_us = float(replication["startTime"]) - auth_end
                add(
                    "generic_worker_processing_latency", trace_id, replication,
                    authentication, delta_us / 1000, delta_us >= 0,
                    trace_failure_reason if delta_us >= 0 and not trace_success else (
                        "" if delta_us >= 0 else "span ordering is inconsistent"
                    ),
                )
                observations[-1]["valid"] = delta_us >= 0 and trace_success

        if "postgresql_session_verification_latency" in requested:
            for span in spans:
                if span.get("operationName") == "db.get_user_by_access_token" and in_window(span):
                    add(
                        "postgresql_session_verification_latency", trace_id, span, None,
                        float(span.get("duration", 0)) / 1000, trace_success,
                        trace_failure_reason,
                    )

        nginx_metrics = {
            "nginx_total_request_latency",
            "nginx_worker_connection_latency",
            "nginx_upstream_first_byte_latency",
            "nginx_upstream_processing_latency",
            "nginx_upstream_response_transfer_latency",
            "nginx_proxy_client_overhead",
            "nginx_proxy_overhead",
        }
        if requested & nginx_metrics:
            processes = trace.get("processes") or {}
            nginx_spans = [
                span for span in spans
                if span.get("operationName") == "matrix-client-proxy"
                and processes.get(span.get("processID"), {}).get(
                    "serviceName", ""
                ).endswith("matrix-nginx")
                and in_window(span)
            ]
            for nginx_span in nginx_spans:
                tags = _tag_map(nginx_span.get("tags"))
                status = int(tags.get("http.status_code", 0) or 0)
                successful = 200 <= status < 300
                general_reason = "" if successful else "HTTP response is not 2xx"
                if "nginx_total_request_latency" in requested:
                    add(
                        "nginx_total_request_latency", trace_id, nginx_span, None,
                        float(nginx_span.get("duration", 0)) / 1000,
                        successful, general_reason,
                    )
                if "nginx_worker_connection_latency" in requested:
                    connect_values = _nginx_time_values(tags.get("nginx.upstream_connect_time"))
                    add(
                        "nginx_worker_connection_latency", trace_id, nginx_span, None,
                        sum(connect_values) * 1000 if connect_values else None,
                        successful and bool(connect_values),
                        general_reason or (
                            "upstream connect time is unavailable" if not connect_values else ""
                        ),
                    )
                connect_values = _nginx_time_values(tags.get("nginx.upstream_connect_time"))
                header_values = _nginx_time_values(tags.get("nginx.upstream_header_time"))
                response_values = _nginx_time_values(tags.get("nginx.upstream_response_time"))

                if "nginx_upstream_first_byte_latency" in requested:
                    add(
                        "nginx_upstream_first_byte_latency", trace_id, nginx_span, None,
                        sum(header_values) * 1000 if header_values else None,
                        successful and bool(header_values),
                        general_reason or (
                            "upstream header time is unavailable" if not header_values else ""
                        ),
                    )

                difference_metrics = (
                    ("nginx_upstream_processing_latency", header_values, connect_values),
                    ("nginx_upstream_response_transfer_latency", response_values, header_values),
                )
                for metric, minuend, subtrahend in difference_metrics:
                    if metric not in requested:
                        continue
                    raw_value = (
                        (sum(minuend) - sum(subtrahend)) * 1000
                        if minuend and subtrahend else None
                    )
                    within_rounding_tolerance = raw_value is not None and raw_value >= -1.1
                    value = max(0.0, raw_value) if within_rounding_tolerance else raw_value
                    add(
                        metric, trace_id, nginx_span, None, value,
                        successful and within_rounding_tolerance,
                        general_reason or (
                            "NGINX timing attributes are unavailable or inconsistent"
                            if not within_rounding_tolerance else ""
                        ),
                        raw_value_ms=raw_value,
                    )

                overhead_metrics = requested & {
                    "nginx_proxy_client_overhead", "nginx_proxy_overhead",
                }
                if overhead_metrics:
                    request_values = _nginx_time_values(tags.get("nginx.request_time"))
                    raw_value = (
                        (request_values[-1] - sum(response_values)) * 1000
                        if request_values and response_values else None
                    )
                    # NGINX exposes these variables with millisecond-level
                    # rounding. A difference down to -1 ms is measurement
                    # noise, not physically negative proxy processing time.
                    within_rounding_tolerance = (
                        raw_value is not None and raw_value >= -1.1
                    )
                    value = max(0.0, raw_value) if within_rounding_tolerance else raw_value
                    valid = successful and within_rounding_tolerance
                    for metric in sorted(overhead_metrics):
                        add(
                            metric, trace_id, nginx_span, None,
                            value, valid,
                            general_reason or (
                                "NGINX timing attributes are unavailable or inconsistent"
                                if not valid else ""
                            ),
                            raw_value_ms=raw_value,
                        )

    return sorted(observations, key=lambda row: (row["timestamp"], row["metric"]))


def aggregate_derived(observations: list[dict], experiment: dict,
                      sample_timestamps: list[str]) -> tuple[list[dict], list[dict]]:
    """Aggregate only causally valid derived observations."""
    pseudo_spans = [
        {"sample": row["sample"], "service": "derived", "operation": row["metric"],
         "endpoint": row.get("endpoint", "other"), "duration_ms": row["value_ms"]}
        for row in observations if row["valid"] is True
    ]
    sample_rows, summary_rows = aggregate_spans(pseudo_spans, experiment, sample_timestamps)

    def synthesize(rows: list[dict], include_sample: bool) -> None:
        key_fields = ["sample"] if include_sample else []
        servlet = {
            tuple(row[field] for field in key_fields): row for row in rows
            if row["scope"] == "message_send"
            and row["operation"] == "_event_persister_servlet_latency"
        }
        transactions = {}
        for preferred_scope in ("other", "message_send"):
            for row in rows:
                if (row["scope"] == preferred_scope
                        and row["operation"] == "event_persistence_transaction_latency"):
                    transactions.setdefault(tuple(row[field] for field in key_fields), row)
        additions = []
        for key in sorted(servlet.keys() & transactions.keys()):
            outer, transaction = servlet[key], transactions[key]
            value = float(outer["mean_ms"]) - float(transaction["mean_ms"])
            record = {
                **{field: outer[field] for field in ("users", "workload", "repetition")},
                "scope": "message_send", "service": "derived",
                "operation": "event_persister_processing_latency",
                "count": min(int(outer["count"]), int(transaction["count"])),
                "mean_ms": value, "median_ms": "", "p95_ms": "",
                "p99_ms": "", "max_ms": "",
            }
            if include_sample:
                record.update({"sample": outer["sample"], "timestamp": outer["timestamp"]})
            additions.append(record)
        rows[:] = [row for row in rows
                   if row["operation"] != "_event_persister_servlet_latency"] + additions

    synthesize(sample_rows, True)
    synthesize(summary_rows, False)
    return sample_rows, summary_rows


def _aggregate(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values), "mean_ms": mean(values), "median_ms": median(values),
        "p95_ms": percentile(values, 0.95), "p99_ms": percentile(values, 0.99),
        "max_ms": max(values),
    }


def aggregate_spans(spans: list[dict], experiment: dict,
                    sample_timestamps: list[str]) -> tuple[list[dict], list[dict]]:
    by_sample: dict[tuple, list[float]] = defaultdict(list)
    by_run: dict[tuple, list[float]] = defaultdict(list)
    for span in spans:
        endpoint = span.get("endpoint", "other")
        for scope in scopes_for_endpoint(endpoint):
            by_sample[(span["sample"], scope, span["service"], span["operation"])].append(
                float(span["duration_ms"])
            )
            by_run[(scope, span["service"], span["operation"])].append(
                float(span["duration_ms"])
            )

    common = {key: experiment[key] for key in ("users", "workload", "repetition")}
    sample_rows = []
    for (sample, scope, service, operation), values in sorted(by_sample.items()):
        sample_rows.append({
            **common, "sample": sample, "timestamp": sample_timestamps[sample - 1],
            "scope": scope, "service": service, "operation": operation,
            **_aggregate(values),
        })
    summary_rows = [
        {**common, "scope": scope, "service": service, "operation": operation,
         **_aggregate(values)}
        for (scope, service, operation), values in sorted(by_run.items())
    ]
    return sample_rows, summary_rows


def write_csv(path: Path, rows: list[dict], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_jaeger_artifacts(directory: Path, spans: list[dict], sample_rows: list[dict],
                           summary_rows: list[dict], metadata: dict,
                           derived_observations: list[dict] | None = None,
                           derived_samples: list[dict] | None = None,
                           derived_summary: list[dict] | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    write_csv(directory / "spans.csv", spans, SPAN_FIELDS)
    write_csv(directory / "samples.csv", sample_rows, AGGREGATE_FIELDS)
    summary_fields = tuple(field for field in AGGREGATE_FIELDS if field not in {"sample", "timestamp"})
    write_csv(directory / "repetition_summary.csv", summary_rows, summary_fields)
    if derived_observations is not None:
        write_csv(directory / "derived_observations.csv", derived_observations, DERIVED_FIELDS)
        write_csv(directory / "derived_samples.csv", derived_samples or [], AGGREGATE_FIELDS)
        write_csv(
            directory / "derived_repetition_summary.csv", derived_summary or [], summary_fields
        )
    (directory / "collection_metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )


def write_quality_artifacts(directory: Path, observations: list[dict],
                            summary_rows: list[dict], metadata: dict,
                            thresholds: dict[str, float]) -> dict:
    """Write a conservative per-run quality assessment for derived metrics."""
    requested = metadata.get("derived_metrics") or []
    common = {
        key: (observations[0].get(key) if observations else metadata.get(key, ""))
        for key in ("users", "workload", "repetition")
    }
    summaries = {
        row["operation"]: row for row in summary_rows
        if row.get("scope", "all") == "all"
    }
    for row in summary_rows:
        if row.get("scope") == "message_send":
            summaries.setdefault(row["operation"], row)
    rows = []
    auth_valid = sum(
        row["metric"] == "request_authentication_latency" and row["valid"] is True
        for row in observations
    )

    severity = {"PASS": 0, "WARNING": 1, "FAIL": 2}
    overall = "PASS"
    for metric in requested:
        metric_rows = [row for row in observations if row["metric"] == metric]
        valid_rows = [row for row in metric_rows if row["valid"] is True]
        invalid = len(metric_rows) - len(valid_rows)
        invalid_percent = 100 * invalid / len(metric_rows) if metric_rows else 100.0
        coverage: float | str = ""
        quality = "PASS"
        reasons = []
        summary = summaries.get(metric, {})
        if metric == "event_persister_processing_latency" and summary:
            # T6 is a run-level difference of means, so it intentionally has
            # no span-level observations of its own.
            valid_rows = [summary]
            invalid = 0
            invalid_percent = 0.0
            if float(summary.get("mean_ms", 0)) < 0:
                quality = "FAIL"
                reasons.append("estimated T6 is negative")
        if not valid_rows:
            quality, reasons = "FAIL", ["no valid observations"]
        elif invalid_percent > thresholds["fail_invalid_percent"]:
            quality = "FAIL"
            reasons.append("invalid observations exceed fail threshold")
        elif invalid_percent > thresholds["warn_invalid_percent"]:
            quality = "WARNING"
            reasons.append("invalid observations exceed warning threshold")

        if (metric == "postgresql_session_verification_latency"
                and "request_authentication_latency" in requested):
            coverage = 100 * len(valid_rows) / auth_valid if auth_valid else 0.0
            if coverage < thresholds["fail_t4_coverage_percent"]:
                quality = "FAIL"
                reasons.append("T5 database-validation coverage is below fail threshold")
            elif coverage < thresholds["warn_t4_coverage_percent"] and quality != "FAIL":
                quality = "WARNING"
                reasons.append("T5 database-validation coverage is below warning threshold")

        if metric in {"nginx_proxy_client_overhead", "nginx_worker_connection_latency"}:
            zero_percent = 100 * sum(float(row["value_ms"]) == 0 for row in valid_rows) / len(valid_rows)
            if zero_percent >= thresholds["warn_zero_percent"] and quality == "PASS":
                quality = "WARNING"
                reasons.append(
                    f"{zero_percent:.1f}% of values are zero or below NGINX timing resolution"
                )

        row = {
            **common, "metric": metric,
            "valid_observations": len(valid_rows),
            "invalid_observations": invalid,
            "invalid_percent": invalid_percent,
            "coverage_percent": coverage,
            "mean_ms": summary.get("mean_ms", ""),
            "p95_ms": summary.get("p95_ms", ""),
            "quality": quality,
            "reason": "; ".join(dict.fromkeys(reasons)),
        }
        rows.append(row)
        if severity[quality] > severity[overall]:
            overall = quality

    if metadata.get("saturated_queries"):
        overall = "FAIL"
    write_csv(directory / "data_quality.csv", rows, QUALITY_FIELDS)
    lines = [
        "# Jaeger data quality", "", f"Overall result: **{overall}**", "",
        "| Metric | Valid | Invalid | Invalid % | Coverage % | Quality | Reason |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        coverage_text = (
            f"{row['coverage_percent']:.1f}" if isinstance(row["coverage_percent"], float)
            else "—"
        )
        lines.append(
            f"| `{row['metric']}` | {row['valid_observations']} | "
            f"{row['invalid_observations']} | {row['invalid_percent']:.1f} | "
            f"{coverage_text} | {row['quality']} | {row['reason'] or '—'} |"
        )
    lines.extend([
        "", "## Interpretation", "",
        "PASS indicates usable observations under the configured thresholds. "
        "WARNING requires explicit interpretation. FAIL means the affected metric "
        "must not be used for inference without correcting the cause.", "",
        "Temporal samples and spans are not independent repetitions. Confidence "
        "intervals, ANOVA and Tukey HSD use run-level summaries.", "",
    ])
    (directory / "DATA_QUALITY.md").write_text("\n".join(lines), encoding="utf-8")
    return {"overall": overall, "rows": rows, "thresholds": thresholds}
