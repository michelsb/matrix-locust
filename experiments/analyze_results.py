#!/usr/bin/env python3
"""Generate factorial plots, 95% confidence intervals and ANOVA tables."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matrix-locust-matplotlib")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from scipy.stats import sem, t
from statsmodels.formula.api import ols
from statsmodels.stats.anova import anova_lm
from statsmodels.stats.multicomp import pairwise_tukeyhsd


METRICS = {
    "message_interarrival_time_ms": "Message injection inter-arrival time (ms)",
    "request_interarrival_all_ms": "All-request start inter-arrival time (ms)",
    "request_interarrival_foreground_ms": "Foreground request-start inter-arrival time (excluding /sync, ms)",
    "request_interarrival_sync_ms": "Matrix /sync request-start inter-arrival time (ms)",
    "request_interarrival_text_send_ms": "Matrix text-send request-start inter-arrival time (ms)",
    "request_interarrival_image_send_ms": "Matrix image-send request-start inter-arrival time (ms)",
    "request_interarrival_media_upload_ms": "Matrix media-upload request-start inter-arrival time (ms)",
    "request_interarrival_other_ms": "Other request-start inter-arrival time (ms)",
    "rps": "Locust RPS",
    "avg_response_time_ms": "Locust average response time (ms)",
    "p95_response_time_ms": "Locust p95 response time (ms)",
    "failures_per_second": "Locust failures/s",
    "foreground_rps": "Locust foreground RPS (excluding /sync)",
    "foreground_failures_per_second": "Locust foreground failures/s (excluding /sync)",
    "foreground_avg_response_time_ms": "Locust foreground average response time (excluding /sync, ms)",
    "sync_rps": "Matrix /sync RPS",
    "sync_avg_response_time_ms": "Matrix /sync average response time (ms)",
    "sync_p95_response_time_ms": "Matrix /sync p95 response time (ms)",
    "text_send_rps": "Matrix text-send RPS",
    "text_send_avg_response_time_ms": "Matrix text-send average response time (ms)",
    "text_send_p95_response_time_ms": "Matrix text-send p95 response time (ms)",
    "image_send_rps": "Matrix image-event RPS",
    "image_send_avg_response_time_ms": "Matrix image-event average response time (ms)",
    "image_send_p95_response_time_ms": "Matrix image-event p95 response time (ms)",
    "media_upload_rps": "Matrix media-upload RPS",
    "media_upload_avg_response_time_ms": "Matrix media-upload average response time (ms)",
    "media_upload_p95_response_time_ms": "Matrix media-upload p95 response time (ms)",
    "cpu_total_percent": "Synapse CPU (% of one core)",
    "memory_total_mib": "Synapse RSS memory (MiB)",
    "synapse_rps": "Synapse HTTP responses/s",
    "synapse_http_errors_per_second": "Synapse HTTP errors/s",
    "synapse_http_p95_ms": "Synapse HTTP p95 (ms)",
    "reactor_tick_p95_ms": "Twisted reactor tick p95 (ms)",
    "db_query_p95_ms": "Database query p95 (ms)",
    "db_schedule_p95_ms": "Database scheduling p95 (ms)",
    "events_persisted_per_second": "Events persisted/s",
    "db_threadpool_utilization_percent": "Database threadpool utilization (%)",
    "replication_events_queue": "Replication events queue",
    "notifier_users": "Notifier users",
    "open_fds_percent": "Open file descriptors (%)",
    "gc_time_ms_per_second": "GC time (ms/s)",
}

# Accept datasets produced before the response-time terminology was corrected.
# Analysis artifacts are always emitted with the current column names.
LEGACY_METRIC_NAMES = {
    metric.replace("response_time", "latency"): metric
    for metric in METRICS
    if "response_time" in metric
}

PLOT_TITLES = True
PLOT_FONT_SIZE = 10.0


def configure_plot_style(no_titles: bool = False,
                         font_size: float | None = None) -> None:
    """Configure presentation without changing the underlying analysis."""
    global PLOT_TITLES, PLOT_FONT_SIZE
    PLOT_TITLES = not no_titles
    if font_size is not None:
        PLOT_FONT_SIZE = font_size
        plt.rcParams.update({
            "font.size": font_size,
            "axes.labelsize": font_size,
            "xtick.labelsize": font_size,
            "ytick.labelsize": font_size,
            "legend.fontsize": font_size,
            "legend.title_fontsize": font_size,
        })


def plot_title(value: str) -> str | None:
    return value if PLOT_TITLES else None


def safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value)


def workload_label(value: str) -> str:
    return {
        "text_only": "Text only",
        "text_and_image": "Text and image",
    }.get(value, value.replace("_", " ").title())


def confidence_interval(values: pd.Series) -> tuple[float, float, float, int]:
    values = values.dropna().astype(float)
    count = len(values)
    mean = values.mean()
    if count < 2:
        return mean, float("nan"), float("nan"), count
    margin = t.ppf(0.975, count - 1) * sem(values)
    return mean, mean - margin, mean + margin, count


def generate_latency_path_coverage(data: pd.DataFrame, output_dir: Path,
                                   plots_dir: Path) -> None:
    """Build the T2–T7 decomposition per independent repetition."""
    mapping = {
        "T2": (("message_send",), "nginx_proxy_client_overhead"),
        "T3": (("message_send",), "nginx_worker_connection_latency"),
        "T4": (("message_send",), "generic_worker_processing_latency"),
        "T5": (("message_send",), "postgresql_session_verification_latency"),
        "T6": (("message_send",), "event_persister_processing_latency"),
        "T7": (("other", "message_send"), "event_persistence_transaction_latency"),
        "nginx_total_request_latency_ms": (
            ("message_send",), "nginx_total_request_latency"
        ),
    }
    keys = ["users", "workload", "repetition"]
    merged = None
    for column, (scopes, operation) in mapping.items():
        part = data[data["scope"].isin(scopes) & (data["operation"] == operation)][
            keys + ["scope", "mean_ms"]
        ].copy()
        part["scope"] = pd.Categorical(part["scope"], categories=scopes, ordered=True)
        part = part.sort_values("scope").drop_duplicates(keys)[keys + ["mean_ms"]]
        part = part.rename(columns={"mean_ms": column})
        merged = part if merged is None else merged.merge(part, on=keys, how="outer")
    if merged is None or merged.empty:
        return
    stage_columns = [f"T{number}" for number in range(2, 8)]
    merged["complete"] = merged[[*stage_columns, "nginx_total_request_latency_ms"]].notna().all(axis=1)
    complete = merged[merged["complete"]].copy()
    if not complete.empty:
        complete["sum_t2_t7_ms"] = complete[stage_columns].sum(axis=1)
        complete["uncovered_ms"] = (
            complete["nginx_total_request_latency_ms"] - complete["sum_t2_t7_ms"]
        )
        complete["coverage_percent"] = (
            100 * complete["sum_t2_t7_ms"]
            / complete["nginx_total_request_latency_ms"]
        )
    merged.to_csv(output_dir / "t2_t7_by_repetition.csv", index=False)
    if complete.empty:
        return
    summary = []
    for (users, workload), group in complete.groupby(["users", "workload"]):
        for metric in [*stage_columns, "sum_t2_t7_ms", "nginx_total_request_latency_ms",
                       "uncovered_ms", "coverage_percent"]:
            value, low, high, count = confidence_interval(group[metric])
            summary.append({
                "users": users, "workload": workload, "metric": metric,
                "n": count, "mean": value, "ci95_low": low, "ci95_high": high,
            })
    pd.DataFrame(summary).to_csv(
        output_dir / "t2_t7_coverage_confidence_intervals.csv", index=False
    )

    figure, axis = plt.subplots(figsize=(9, 5))
    coverage = pd.DataFrame(summary)
    coverage = coverage[coverage["metric"] == "coverage_percent"].sort_values("users")
    for workload, group in coverage.groupby("workload"):
        axis.errorbar(
            group["users"], group["mean"],
            yerr=[(group["mean"] - group["ci95_low"]).fillna(0),
                  (group["ci95_high"] - group["mean"]).fillna(0)],
            marker="o", capsize=5, label=workload_label(workload),
        )
    axis.set_xticks(sorted(coverage["users"].unique()))
    axis.set(xlabel="Concurrent users", ylabel="Explained latency (%)",
             title=plot_title("T2–T7 latency coverage by independent run"))
    axis.grid(alpha=0.3)
    axis.legend(title="Workload")
    figure.tight_layout()
    figure.savefig(plots_dir / "t2_t7_coverage.png", dpi=160)
    plt.close(figure)


def generate_jaeger_combined_plots(data: pd.DataFrame, output_dir: Path,
                                   plots_dir: Path,
                                   metrics: dict[str, str], scope: str = "all") -> None:
    """Compare every selected Jaeger operation in a single figure."""
    data = data[data["scope"] == scope]
    suffix = "" if scope == "all" else f"_{safe_name(scope)}"
    records = []
    for (service, operation, users, workload), group in data.groupby(
        ["service", "operation", "users", "workload"]
    ):
        for statistic in metrics:
            value, low, high, count = confidence_interval(group[statistic])
            if pd.isna(value):
                continue
            records.append({
                "service": service, "operation": operation,
                "statistic": statistic, "users": users,
                "workload": workload, "n": count, "mean": value,
                "ci95_low": low, "ci95_high": high,
            })
    combined = pd.DataFrame(records)
    if combined.empty:
        return

    duplicate_names = combined.groupby("operation")["service"].nunique()
    combined["display_metric"] = combined.apply(
        lambda row: (
            f"{row['operation']} — {row['service']}"
            if duplicate_names[row["operation"]] > 1 else row["operation"]
        ), axis=1,
    )
    baselines = combined.groupby(["service", "operation", "statistic"])["mean"].transform("mean")
    combined["normalized_mean"] = combined["mean"] / baselines
    combined["normalized_ci95_low"] = combined["ci95_low"] / baselines
    combined["normalized_ci95_high"] = combined["ci95_high"] / baselines
    combined.insert(0, "scope", scope)
    combined.to_csv(output_dir / f"all_metrics_by_scenario{suffix}.csv", index=False)

    def draw(statistic: str, normalized: bool = False) -> None:
        frame = combined[combined["statistic"] == statistic].copy()
        metric_names = list(dict.fromkeys(frame["display_metric"]))
        scenarios = sorted(
            frame[["users", "workload"]].drop_duplicates().itertuples(index=False),
            key=lambda scenario: (scenario.users, scenario.workload),
        )
        figure_height = max(5.5, len(metric_names) * 0.65)
        figure, axis = plt.subplots(figsize=(13, figure_height))
        offsets = (
            [0.0] if len(scenarios) == 1
            else [((index / (len(scenarios) - 1)) - 0.5) * 0.6
                  for index in range(len(scenarios))]
        )
        for offset, scenario in zip(offsets, scenarios):
            subset = frame[
                (frame["users"] == scenario.users)
                & (frame["workload"] == scenario.workload)
            ].set_index("display_metric")
            subset = subset.reindex(metric_names)
            value_column = "normalized_mean" if normalized else "mean"
            low_column = "normalized_ci95_low" if normalized else "ci95_low"
            high_column = "normalized_ci95_high" if normalized else "ci95_high"
            values = subset[value_column]
            low_errors = (values - subset[low_column]).fillna(0).clip(lower=0)
            high_errors = (subset[high_column] - values).fillna(0).clip(lower=0)
            axis.errorbar(
                values, [index + offset for index in range(len(metric_names))],
                xerr=[low_errors, high_errors], marker="o", linestyle="none",
                capsize=4,
                label=f"{scenario.users:g} / {workload_label(scenario.workload)}",
            )
        axis.set_yticks(range(len(metric_names)), metric_names)
        if normalized:
            axis.axvline(1.0, color="black", linewidth=1, alpha=0.5)
            axis.set(
                title=plot_title(f"All Jaeger metrics by scenario — {scope} — normalized run mean"),
                xlabel="Relative value (metric-wide mean = 1)",
                ylabel="Jaeger metric / operation",
            )
            filename = f"all_metrics{suffix}_normalized_mean.png"
        else:
            if (frame["mean"] > 0).all():
                axis.set_xscale("log")
            axis.set(
                title=plot_title(f"All Jaeger metrics by scenario — {scope} — {metrics[statistic]}"),
                xlabel=f"{metrics[statistic]} (log scale)",
                ylabel="Jaeger metric / operation",
            )
            filename = f"all_metrics{suffix}_{statistic}.png"
        axis.grid(axis="x", alpha=0.3)
        axis.legend(title="Users / workload", bbox_to_anchor=(1.02, 1), loc="upper left")
        figure.tight_layout()
        figure.savefig(plots_dir / filename, dpi=180, bbox_inches="tight")
        plt.close(figure)

    for statistic in metrics:
        draw(statistic)
    draw("mean_ms", normalized=True)


def generate_cpu_workers_plot(run_summaries: pd.DataFrame, output_dir: Path,
                              plots_dir: Path) -> None:
    """Plot every discovered worker as a stacked contribution per scenario."""
    job_columns = sorted(
        column for column in run_summaries.columns
        if column.startswith("cpu_") and column.endswith("_percent")
        and column != "cpu_total_percent"
    )
    if not job_columns:
        return

    scenarios = sorted(
        run_summaries[["users", "workload"]].drop_duplicates().itertuples(index=False),
        key=lambda scenario: (scenario.users, scenario.workload),
    )
    means = run_summaries.groupby(["users", "workload"])[job_columns].mean()
    records = []
    for scenario in scenarios:
        key = (scenario.users, scenario.workload)
        scenario_total = means.loc[key].sum()
        for column in job_columns:
            value = means.loc[key, column]
            records.append({
                "users": scenario.users,
                "workload": scenario.workload,
                "job": column.removeprefix("cpu_").removesuffix("_percent"),
                "mean_cpu_percent": value,
                "scenario_total_cpu_percent": scenario_total,
                "share_percent": value / scenario_total * 100 if scenario_total else 0,
            })
    pd.DataFrame(records).to_csv(output_dir / "cpu_workers_by_scenario.csv", index=False)

    figure, axis = plt.subplots(figsize=(13, 7))
    x_values = list(range(len(scenarios)))
    bottoms = [0.0] * len(scenarios)
    colors = plt.get_cmap("tab20").colors
    for index, column in enumerate(job_columns):
        values = [means.loc[(scenario.users, scenario.workload), column]
                  for scenario in scenarios]
        axis.bar(
            x_values, values, bottom=bottoms,
            label=column.removeprefix("cpu_").removesuffix("_percent"),
            color=colors[index % len(colors)], width=0.72,
        )
        bottoms = [bottom + value for bottom, value in zip(bottoms, values)]

    total_means = []
    errors_low = []
    errors_high = []
    for scenario, stacked_total in zip(scenarios, bottoms):
        group = run_summaries[
            (run_summaries["users"] == scenario.users)
            & (run_summaries["workload"] == scenario.workload)
        ]
        source = group["cpu_total_percent"] if "cpu_total_percent" in group else pd.Series([stacked_total])
        mean, low, high, _count = confidence_interval(source)
        total_means.append(mean)
        errors_low.append(max(0, mean - low) if pd.notna(low) else 0)
        errors_high.append(max(0, high - mean) if pd.notna(high) else 0)
    axis.errorbar(
        x_values, total_means, yerr=[errors_low, errors_high], fmt="none",
        ecolor="black", elinewidth=1.2, capsize=4, label="Total CPU — 95% CI",
    )
    for x_value, total, error in zip(x_values, total_means, errors_high):
        axis.text(x_value, total + error + 3, f"{total:.1f}%", ha="center", va="bottom",
                  fontsize=PLOT_FONT_SIZE)

    labels = [
        f"{scenario.users}\n{workload_label(scenario.workload)}"
        for scenario in scenarios
    ]
    axis.set_xticks(x_values, labels)
    axis.set(
        title=plot_title("Synapse CPU by worker and factorial scenario"),
        xlabel="Concurrent users / workload",
        ylabel="CPU (% of one core)",
    )
    axis.grid(axis="y", alpha=0.25)
    axis.legend(title="Worker", bbox_to_anchor=(1.02, 1), loc="upper left")
    figure.tight_layout()
    figure.savefig(plots_dir / "cpu_workers_stacked.png", dpi=180, bbox_inches="tight")
    plt.close(figure)


def generate_analysis(samples_csv: Path, output_dir: Path,
                      no_plot_titles: bool = False,
                      plot_font_size: float | None = None) -> None:
    configure_plot_style(no_plot_titles, plot_font_size)
    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"
    plots_dir.mkdir(exist_ok=True)
    data = pd.read_csv(samples_csv)
    legacy_columns = {
        old_name: new_name
        for old_name, new_name in LEGACY_METRIC_NAMES.items()
        if old_name in data and new_name not in data
    }
    if legacy_columns:
        data = data.rename(columns=legacy_columns)
    data["users"] = pd.to_numeric(data["users"])
    metric_labels = dict(METRICS)
    for column in data.columns:
        if (column.startswith("cpu_") and column.endswith("_percent")
                and column not in metric_labels):
            job = column.removeprefix("cpu_").removesuffix("_percent")
            metric_labels[column] = f"Synapse CPU: {job} (% of one core)"
    available_metrics = [metric for metric in metric_labels if metric in data]
    for metric in available_metrics:
        data[metric] = pd.to_numeric(data[metric], errors="coerce")
    run_summaries = (
        data.groupby(["users", "workload", "repetition"], as_index=False)[available_metrics]
        .mean(numeric_only=True)
    )
    run_summaries.to_csv(output_dir / "run_summaries.csv", index=False)
    generate_cpu_workers_plot(run_summaries, output_dir, plots_dir)
    ci_rows = []
    coefficient_rows = []
    skipped_inference_rows = []
    tukey_dir = output_dir / "tukey"
    tukey_plots_dir = plots_dir / "tukey"
    tukey_dir.mkdir(exist_ok=True)
    tukey_plots_dir.mkdir(exist_ok=True)

    for metric, label in metric_labels.items():
        if metric not in data or data[metric].dropna().empty:
            continue
        frame = run_summaries[["users", "workload", "repetition", metric]].copy()
        frame = frame.dropna(subset=[metric])

        summary = []
        for (users, workload), group in frame.groupby(["users", "workload"]):
            mean, low, high, count = confidence_interval(group[metric])
            record = {"metric": metric, "users": users, "workload": workload,
                      "n": count, "mean": mean, "ci95_low": low, "ci95_high": high}
            ci_rows.append(record)
            summary.append(record)

        figure, axis = plt.subplots(figsize=(8, 5))
        summary_df = pd.DataFrame(summary)
        for workload, group in summary_df.groupby("workload"):
            group = group.sort_values("users")
            axis.errorbar(
                group["users"], group["mean"],
                yerr=[group["mean"] - group["ci95_low"], group["ci95_high"] - group["mean"]],
                marker="o", capsize=5, label=workload_label(workload),
            )
        loads = sorted(summary_df["users"].unique())
        axis.set_xticks(loads, [f"{load:g}" for load in loads])
        axis.set(title=plot_title(f"Full factorial: {label}"),
                 xlabel="Concurrent users", ylabel=label)
        axis.grid(alpha=0.3)
        axis.legend(title="Workload")
        figure.tight_layout()
        figure.savefig(plots_dir / f"{safe_name(metric)}.png", dpi=160)
        plt.close(figure)

        enough_replication = frame.groupby(["users", "workload"]).size().min() >= 2
        factorial_coverage = frame["users"].nunique() >= 2 and frame["workload"].nunique() >= 2
        has_variation = frame[metric].nunique() >= 2
        if factorial_coverage and enough_replication and has_variation:
            model = ols(f"Q('{metric}') ~ C(users) * C(workload)", data=frame).fit()
            anova = anova_lm(model, typ=2)
            anova.to_csv(output_dir / f"anova_{safe_name(metric)}.csv", index_label="effect")
            intervals = model.conf_int(alpha=0.05)
            for term, estimate in model.params.items():
                coefficient_rows.append({
                    "metric": metric, "term": term, "estimate": estimate,
                    "ci95_low": intervals.loc[term, 0], "ci95_high": intervals.loc[term, 1],
                    "p_value": model.pvalues[term],
                })

            tukey_groups = (
                frame["users"].astype(str) + " / "
                + frame["workload"].map(workload_label)
            )
            tukey = pairwise_tukeyhsd(frame[metric], tukey_groups, alpha=0.05)
            tukey_table = pd.DataFrame(
                tukey.summary().data[1:], columns=tukey.summary().data[0]
            )
            tukey_table.to_csv(tukey_dir / f"tukey_{safe_name(metric)}.csv", index=False)
            tukey_figure = tukey.plot_simultaneous(
                xlabel=label, ylabel="Users / workload", figsize=(10, 6)
            )
            if PLOT_TITLES:
                tukey_figure.suptitle(f"Tukey HSD simultaneous 95% CI: {label}")
            else:
                tukey_figure.suptitle("")
                for tukey_axis in tukey_figure.axes:
                    tukey_axis.set_title("")
            tukey_figure.tight_layout()
            tukey_figure.savefig(tukey_plots_dir / f"tukey_{safe_name(metric)}.png", dpi=160)
            plt.close(tukey_figure)
        else:
            reasons = []
            if not factorial_coverage:
                reasons.append("metric is not available for both factorial factors")
            if not enough_replication:
                reasons.append("fewer than two independent runs in at least one cell")
            if not has_variation:
                reasons.append("response is constant across independent runs")
            skipped_inference_rows.append({"metric": metric, "reason": "; ".join(reasons)})

    pd.DataFrame(ci_rows).to_csv(output_dir / "confidence_intervals.csv", index=False)
    pd.DataFrame(coefficient_rows).to_csv(output_dir / "anova_coefficients_ci95.csv", index=False)
    pd.DataFrame(skipped_inference_rows).to_csv(
        output_dir / "skipped_inference.csv", index=False
    )
    (output_dir / "STATISTICAL_WARNING.txt").write_text(
        "The 31 temporal samples are averaged into one value per independent "
        "run. Confidence intervals, factorial ANOVA and Tukey HSD use only these "
        "run-level summaries. At least two repetitions per cell are required; "
        "three are the practical minimum and five are recommended when feasible.\n",
        encoding="utf-8",
    )


def generate_jaeger_analysis(summary_csv: Path, output_dir: Path,
                             samples_csv: Path | None = None) -> None:
    """Analyze run-level Jaeger summaries without treating spans as replicates."""
    output_dir.mkdir(parents=True, exist_ok=True)
    plots_dir = output_dir / "plots"
    tukey_dir = output_dir / "tukey"
    tukey_plots_dir = plots_dir / "tukey"
    anova_dir = output_dir / "anova"
    for directory in (plots_dir, tukey_dir, tukey_plots_dir, anova_dir):
        directory.mkdir(parents=True, exist_ok=True)

    data = pd.read_csv(summary_csv)
    if data.empty:
        (output_dir / "NO_DATA.txt").write_text(
            "Jaeger collection produced no selected spans.\n", encoding="utf-8"
        )
        return
    data["users"] = pd.to_numeric(data["users"])
    if "scope" not in data:
        data["scope"] = "all"
    metrics = {
        "mean_ms": "Mean span duration (ms)",
        "p95_ms": "Span duration p95 (ms)",
    }
    for metric in metrics:
        data[metric] = pd.to_numeric(data[metric], errors="coerce")

    generate_latency_path_coverage(data, output_dir, plots_dir)

    for scope in sorted(data["scope"].dropna().unique()):
        generate_jaeger_combined_plots(data, output_dir, plots_dir, metrics, scope)

    ci_rows = []
    coefficient_rows = []
    skipped_rows = []
    for (scope, service, operation), operation_frame in data.groupby(
        ["scope", "service", "operation"]
    ):
        scope_prefix = "" if scope == "all" else f"{scope}__"
        stem = safe_name(f"{scope_prefix}{service}__{operation}")
        for metric, label in metrics.items():
            frame = operation_frame[
                ["users", "workload", "repetition", metric]
            ].dropna(subset=[metric]).copy()
            if frame.empty:
                continue
            summary = []
            for (users, workload), group in frame.groupby(["users", "workload"]):
                value, low, high, count = confidence_interval(group[metric])
                record = {
                    "scope": scope, "service": service, "operation": operation, "metric": metric,
                    "users": users, "workload": workload, "n": count,
                    "mean": value, "ci95_low": low, "ci95_high": high,
                }
                ci_rows.append(record)
                summary.append(record)

            summary_df = pd.DataFrame(summary)
            figure, axis = plt.subplots(figsize=(9, 5))
            for workload, group in summary_df.groupby("workload"):
                group = group.sort_values("users")
                low_error = (group["mean"] - group["ci95_low"]).fillna(0).clip(lower=0)
                high_error = (group["ci95_high"] - group["mean"]).fillna(0).clip(lower=0)
                axis.errorbar(
                    group["users"], group["mean"], yerr=[low_error, high_error],
                    marker="o", capsize=5, label=workload_label(workload),
                )
            loads = sorted(summary_df["users"].unique())
            axis.set_xticks(loads, [f"{load:g}" for load in loads])
            axis.set(
                title=plot_title(f"Jaeger ({scope}): {operation} — {service}"),
                xlabel="Concurrent users", ylabel=label,
            )
            axis.grid(alpha=0.3)
            axis.legend(title="Workload")
            figure.tight_layout()
            figure.savefig(plots_dir / f"{stem}__{metric}.png", dpi=160)
            plt.close(figure)

            cell_sizes = frame.groupby(["users", "workload"]).size()
            enough = not cell_sizes.empty and cell_sizes.min() >= 2
            expected_cells = frame["users"].nunique() * frame["workload"].nunique()
            coverage = (
                frame["users"].nunique() >= 2
                and frame["workload"].nunique() >= 2
                and len(cell_sizes) == expected_cells
            )
            variation = frame[metric].nunique() >= 2
            if enough and coverage and variation:
                model = ols(f"Q('{metric}') ~ C(users) * C(workload)", data=frame).fit()
                anova_lm(model, typ=2).to_csv(
                    anova_dir / f"anova_{stem}__{metric}.csv", index_label="effect"
                )
                intervals = model.conf_int(alpha=0.05)
                for term, estimate in model.params.items():
                    coefficient_rows.append({
                        "scope": scope, "service": service, "operation": operation, "metric": metric,
                        "term": term, "estimate": estimate,
                        "ci95_low": intervals.loc[term, 0],
                        "ci95_high": intervals.loc[term, 1],
                        "p_value": model.pvalues[term],
                    })
                groups = frame["users"].astype(str) + " / " + frame["workload"].map(workload_label)
                tukey = pairwise_tukeyhsd(frame[metric], groups, alpha=0.05)
                pd.DataFrame(
                    tukey.summary().data[1:], columns=tukey.summary().data[0]
                ).to_csv(tukey_dir / f"tukey_{stem}__{metric}.csv", index=False)
                tukey_figure = tukey.plot_simultaneous(
                    xlabel=label, ylabel="Users / workload", figsize=(10, 6)
                )
                if PLOT_TITLES:
                    tukey_figure.suptitle(f"Tukey HSD 95% CI: {operation} — {service}")
                else:
                    tukey_figure.suptitle("")
                    for tukey_axis in tukey_figure.axes:
                        tukey_axis.set_title("")
                tukey_figure.tight_layout()
                tukey_figure.savefig(
                    tukey_plots_dir / f"tukey_{stem}__{metric}.png", dpi=160
                )
                plt.close(tukey_figure)
            else:
                reasons = []
                if not coverage:
                    reasons.append("both factorial factors are not represented")
                if not enough:
                    reasons.append("fewer than two independent runs in a cell")
                if not variation:
                    reasons.append("response is constant")
                skipped_rows.append({
                    "scope": scope, "service": service, "operation": operation, "metric": metric,
                    "reason": "; ".join(reasons),
                })

    pd.DataFrame(ci_rows).to_csv(output_dir / "confidence_intervals.csv", index=False)
    pd.DataFrame(coefficient_rows).to_csv(
        output_dir / "anova_coefficients_ci95.csv", index=False
    )
    pd.DataFrame(skipped_rows).to_csv(output_dir / "skipped_inference.csv", index=False)

    if samples_csv is not None and samples_csv.is_file():
        samples = pd.read_csv(samples_csv)
        if not samples.empty:
            if "scope" not in samples:
                samples["scope"] = "all"
            samples["sample"] = pd.to_numeric(samples["sample"])
            samples["p95_ms"] = pd.to_numeric(samples["p95_ms"], errors="coerce")
            temporal_dir = plots_dir / "temporal"
            temporal_dir.mkdir(exist_ok=True)
            grouped = samples.groupby(
                ["scope", "service", "operation", "users", "workload", "sample"], as_index=False
            )["p95_ms"].mean()
            for (scope, service, operation), operation_frame in grouped.groupby(
                ["scope", "service", "operation"]
            ):
                figure, axis = plt.subplots(figsize=(10, 5))
                for (users, workload), scenario in operation_frame.groupby(["users", "workload"]):
                    scenario = scenario.sort_values("sample")
                    axis.plot(
                        scenario["sample"], scenario["p95_ms"], marker=".",
                        label=f"{users:g} / {workload_label(workload)}",
                    )
                axis.set(
                    title=plot_title(f"Jaeger temporal p95 ({scope}): {operation} — {service}"),
                    xlabel="Measurement sample", ylabel="Span duration p95 (ms)",
                )
                axis.grid(alpha=0.3)
                axis.legend(title="Users / workload")
                figure.tight_layout()
                figure.savefig(
                    temporal_dir / f"{safe_name(scope + '__' + service + '__' + operation)}.png", dpi=160
                )
                plt.close(figure)

    (output_dir / "STATISTICAL_WARNING.txt").write_text(
        "Jaeger spans and temporal samples are not treated as independent replicates. "
        "Each cell contributes one run-level mean or p95 per service and operation to "
        "confidence intervals, factorial ANOVA and Tukey HSD.\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples_csv", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("results/analysis"))
    parser.add_argument("--no-plot-titles", action="store_true",
                        help="Generate figures without chart titles")
    parser.add_argument("--plot-font-size", type=float,
                        help="Base font size for axes, ticks and legends")
    args = parser.parse_args()
    generate_analysis(
        args.samples_csv, args.output_dir,
        no_plot_titles=args.no_plot_titles,
        plot_font_size=args.plot_font_size,
    )


if __name__ == "__main__":
    main()
