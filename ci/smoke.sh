#!/usr/bin/env bash
# Boot probe for a built image, parameterised by tag.
#
# The tag is an ARGUMENT so ci.yml's negative control can run this exact
# script against a deliberately broken image.
#
# set -e carries the failures, so there is no per-command handler to swallow.
set -euo pipefail

tag="${1:?usage: smoke.sh <image-tag> [output-prefix]}"
out="${2:-/tmp/smoke}"

# --network none: the image must not reach pypi at boot. That is the bug this
#   guards -- `uv run` re-resolved the project on a container's first start.
# No --user override: the image must work as the appuser it declares, or a
#   regression to a root-only image would pass unnoticed.
docker run --rm --network none "$tag" --help > "$out.help" 2>&1
grep -q "Cortex operational utilities" "$out.help"

# Proves click subcommand dispatch. It adds no IMPORT coverage: both probes
# load the same 22 cortex_utils modules, measured with -X importtime. Neither
# reaches cortex_utils.introspect, which cli.py imports lazily inside
# functions.
docker run --rm --network none "$tag" queue --help > "$out.queue" 2>&1
