#!/usr/bin/env bash
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${UURB_BUILD_DIR:-$repo_dir/build/winpr}"
output_dir="${1:-$repo_dir/build/freerdp}"
recipe="$repo_dir/vendor/freerdp-sdl-build"
validator="$repo_dir/scripts/verify-freerdp-runtime.py"
python=/usr/bin/python3

# Approval lives in the repository, never in a generated cache manifest.
"$python" "$validator" --mode profile
if "$python" "$validator" "$output_dir" >/dev/null 2>&1; then
    printf 'Approved source-built FreeRDP runtime is current in %s\n' "$output_dir"
    exit 0
fi
# A completed source build may be reused only after checking its fixed product pins.
source_output="${UURB_FREERDP_PREBUILT_DIR:-}"
if [[ -z "$source_output" ]]; then
    mkdir -p "$work_dir"
    work_dir="$(cd -- "$work_dir" && pwd)"
    job="$(mktemp -d "$work_dir/cold-source.XXXXXXXX")"
    printf 'Cold source build: %s (two build jobs, deadline 900s)\n' "$job"
    timeout --signal=TERM --kill-after=5s 900s \
        "$recipe/bootstrap.sh" "$job/source" "$job/output" "$repo_dir"
    source_output="$job/output"
fi
"$python" "$validator" --mode build "$source_output"
# This is a build cache, not a live installation. Interrupted copies fail closed.
while IFS= read -r name; do
    mkdir -p "$output_dir/$(dirname -- "$name")"
    [[ ! -L "$output_dir/$name" ]] || exit 1
    install -m 0755 "$source_output/$name" "$output_dir/$name"
done < <("$python" "$validator" --mode list)
for name in .build-sha256 .build-recipe; do
    [[ ! -L "$output_dir/$name" ]] || exit 1
    install -m 0644 "$source_output/$name" "$output_dir/$name.tmp"
    mv -f "$output_dir/$name.tmp" "$output_dir/$name"
done
"$python" - "$repo_dir/patches/freerdp-sdl-product.json" "$output_dir" <<'PYPROVENANCE'
import hashlib, json, sys
from pathlib import Path
profile_path, output = map(Path, sys.argv[1:])
profile = json.loads(profile_path.read_text())
receipt = {"schema": 1, "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
           "source_lock_sha256": profile["source_lock_sha256"],
           "recipe_digest": (output / ".build-recipe").read_text().strip()}
(output / ".source-provenance.json.tmp").write_text(json.dumps(receipt, sort_keys=True) + "\n")
(output / ".source-provenance.json.tmp").replace(output / ".source-provenance.json")
PYPROVENANCE
"$python" "$validator" "$output_dir"
printf 'Approved thirteen-PE source runtime cached in %s\n' "$output_dir"
