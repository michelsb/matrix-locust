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
        axis.text(x_value, total + error + 3, f"{total:.1f}%", ha="center", va="bottom", fontsize=9)

    labels = [
        f"{scenario.users}\n{workload_label(scenario.workload)}"
        for scenario in scenarios
    ]
    axis.set_xticks(x_values, labels)
    axis.set(
        title="Synapse CPU by worker and factorial scenario",
        xlabel="Concurrent users / workload",
        ylabel="CPU (% of one core)",
    )
    axis.grid(axis="y", alpha=0.25)
    axis.legend(title="Worker", bbox_to_anchor=(1.02, 1), loc="upper left")
    figure.tight_layout()
    figure.savefig(plots_dir / "cpu_workers_stacked.png", dpi=180, bbox_inches="tight")
    plt.close(figure)


def generate_analysis(samples_csv: Path, output_dir: Path) -> None:
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
        axis.set(title=f"Full factorial: {label}", xlabel="Concurrent users", ylabel=label)
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
            tukey_figure.suptitle(f"Tukey HSD simultaneous 95% CI: {label}")
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples_csv", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("results/analysis"))
    args = parser.parse_args()
    generate_analysis(args.samples_csv, args.output_dir)


if __name__ == "__main__":
    main()
