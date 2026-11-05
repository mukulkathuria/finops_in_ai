#!/usr/bin/env bash
# Run one load profile with the pinned k6 image against the local stack, and add its summary to
# the GitHub job summary when there is one.
#
#   tests/load/k6/run.sh <baseline|spike|race|failover> [k6 options, e.g. -e VUS=100]
set -euo pipefail

profile="${1:?usage: tests/load/k6/run.sh <profile> [k6 options]}"
shift
image="${K6_IMAGE:-grafana/k6:2.3.0}"
here="$(cd "$(dirname "$0")" && pwd)"
out="$(mktemp)"
status=0
docker run --rm --network host -v "$here:/k6:ro" "$image" run "$@" "/k6/$profile.js" 2>&1 |
  tee "$out" || status=$?
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### $profile (exit $status)"
    echo '```'
    grep -E "[✓✗]|gateway_overhead_ms|errors|unexpected_answers|http_reqs|iterations|committed" "$out" |
      tail -40
    echo '```'
  } >>"$GITHUB_STEP_SUMMARY"
fi
exit "$status"
