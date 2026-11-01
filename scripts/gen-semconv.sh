#!/usr/bin/env bash
# ABOUTME: Regenerates internal/telemetry/semconv from the workshop repo's weaver span registry, at a
# ABOUTME: given git ref, and records which commit it came from. Needs weaver and a workshop checkout.
set -euo pipefail

usage="usage: scripts/gen-semconv.sh <Unleash_an_Agent_Watch_It_Burn checkout> [git ref, default origin/main]"
workshop="${1:?$usage}"
ref="${2:-origin/main}"
repo="$(git rev-parse --show-toplevel)"
out="$repo/internal/telemetry/semconv"

commit="$(git -C "$workshop" rev-parse --short "$ref")"
work="$(mktemp -d -p "$repo")"
trap 'rm -r "$work"' EXIT

echo "reading weaver/registry from $workshop at $ref ($commit)"
git -C "$workshop" archive "$ref" weaver/registry | tar -x -C "$work"
weaver registry generate -r "$work/weaver/registry" -t "$repo/weaver/templates" \
  --param source="Unleash_an_Agent_Watch_It_Burn $commit" go "$work/out"
gofmt -w "$work/out/attributes_gen.go"
cp "$work/out/attributes_gen.go" "$out/attributes_gen.go"

if [ -s "$out/attributes_gen.go" ] && grep -q 'WitbGuardDecisionKey' "$out/attributes_gen.go"; then
  echo "PASS wrote $out/attributes_gen.go from $commit"
else
  echo "FAIL $out/attributes_gen.go is missing the guard decision key"
  exit 1
fi
