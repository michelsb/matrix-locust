#!/usr/bin/env bash

# Run one low-load, eight-hour cell against each side of the federation.
# Configuration can be overridden through the environment; see
# docs/experiments/federation.md.

set -u

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [[ -f setup_federation/.env ]]; then
  # shellcheck disable=SC1091
  source setup_federation/.env
fi

: "${FED_HOME01_URL:?Define FED_HOME01_URL in setup_federation/.env}"
: "${FED_HOME02_URL:?Define FED_HOME02_URL in setup_federation/.env}"

users_per_homeserver="${USERS_PER_HOMESERVER:-10}"
spawn_rate="${SPAWN_RATE:-2}"
total_seconds="${TOTAL_SECONDS:-28800}"
stabilization="${STABILIZATION_SECONDS:-60}"
collection_buffer="${COLLECTION_BUFFER_SECONDS:-5}"
samples="${SAMPLES:-480}"
message_rate="${MESSAGE_RATE:-0.05}"
image_ratio="${IMAGE_RATIO:-0.15}"
run_id="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
result_root="${OUTPUT_ROOT:-results/federation-soak-8h/$run_id}"

# run_factorial includes ramp-up and the collection buffer in Locust's runtime.
# Subtract them so each process produces load for exactly TOTAL_SECONDS.
ramp_seconds=$(awk -v users="$users_per_homeserver" -v rate="$spawn_rate" \
  'BEGIN { print int((users + rate - 0.000001) / rate) }')
measurement_duration=$((total_seconds - ramp_seconds - stabilization - collection_buffer))

if (( measurement_duration <= 0 )); then
  echo "TOTAL_SECONDS must exceed ramp-up + stabilization + collection buffer" >&2
  exit 2
fi

prometheus_home01=(--no-prometheus)
prometheus_home02=(--no-prometheus)
if [[ "${COLLECT_PROMETHEUS:-false}" == "true" ]]; then
  : "${PROMETHEUS_URL:?Define PROMETHEUS_URL when COLLECT_PROMETHEUS=true}"
  home01_instance="${HOME01_PROMETHEUS_INSTANCE:-${FED_HOME01_DOMAIN:-}}"
  home02_instance="${HOME02_PROMETHEUS_INSTANCE:-${FED_HOME02_DOMAIN:-}}"
  : "${home01_instance:?Define HOME01_PROMETHEUS_INSTANCE or FED_HOME01_DOMAIN}"
  : "${home02_instance:?Define HOME02_PROMETHEUS_INSTANCE or FED_HOME02_DOMAIN}"
  prometheus_home01=(--prometheus-url "$PROMETHEUS_URL" --instance "$home01_instance")
  prometheus_home02=(--prometheus-url "$PROMETHEUS_URL" --instance "$home02_instance")
fi

common_args=(
  --loads "$users_per_homeserver"
  --workloads text_and_image
  --spawn-rate "$spawn_rate"
  --message-rate "$message_rate"
  --image-ratio "$image_ratio"
  --text-length-profile mixed
  --sync-timeout 30
  --repetitions 1
  --stabilization "$stabilization"
  --measurement-duration "$measurement_duration"
  --collection-buffer "$collection_buffer"
  --samples "$samples"
  --cooldown 0
)

echo "Federated soak test: $users_per_homeserver users per homeserver, ${total_seconds}s total"
echo "Measurement window: ${measurement_duration}s; output: $result_root"

# Each side gets its own process group. This lets the signal handler terminate
# Poetry, the Python runner and any other descendants together.
setsid poetry run python experiments/run_factorial.py \
  --host "$FED_HOME01_URL" \
  --data-dir data/federation/exports/home01 \
  --output-dir "$result_root/home01" \
  "${common_args[@]}" "${prometheus_home01[@]}" &
pid_home01=$!

setsid poetry run python experiments/run_factorial.py \
  --host "$FED_HOME02_URL" \
  --data-dir data/federation/exports/home02 \
  --output-dir "$result_root/home02" \
  "${common_args[@]}" "${prometheus_home02[@]}" &
pid_home02=$!

stop_children() {
  local signal_name="${1:-TERM}"
  kill -s "$signal_name" -- "-$pid_home01" "-$pid_home02" 2>/dev/null || true
}

handle_interruption() {
  local signal_name="$1"
  trap - INT TERM
  echo
  echo "Signal $signal_name received; stopping both homeservers..." >&2
  stop_children TERM

  # Give run_factorial time to terminate its separate Locust process group.
  for _ in {1..10}; do
    if ! kill -0 "$pid_home01" 2>/dev/null && ! kill -0 "$pid_home02" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  stop_children KILL
  wait "$pid_home01" 2>/dev/null || true
  wait "$pid_home02" 2>/dev/null || true
  echo "Both federated load processes were stopped." >&2
  exit 130
}

trap 'handle_interruption INT' INT
trap 'handle_interruption TERM' TERM

wait "$pid_home01"
status_home01=$?
wait "$pid_home02"
status_home02=$?
trap - INT TERM

echo "Federated soak test finished: home01=$status_home01 home02=$status_home02"
echo "Results: $result_root"

if (( status_home01 != 0 || status_home02 != 0 )); then
  exit 1
fi
