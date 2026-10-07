#!/usr/bin/env bash
set -Eeuo pipefail
export PATH=/usr/bin:/bin
recipe_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
p="${1:?usage: bootstrap.sh FRESH_BUILD_ROOT FRESH_OUTPUT_DIR PRODUCT_REPO}"
output="${2:?output directory required}"
product_repo="${3:?source repository required}"
[[ "$p" == /* && "$output" == /* && "$product_repo" == /* ]] || exit 2
[[ ! -e "$p" && ! -L "$p" && ! -e "$output" && ! -L "$output" ]] || {
    printf 'Cold build root and output must both be fresh; existing cache remains read-only.\n' >&2; exit 1;
}
export UURB_SOURCE_BUILD_ROOT="$p" UURB_BUILD_RECIPE_DIR="$recipe_dir" UURB_PRODUCT_REPO="$product_repo" SOURCE_DATE_EPOCH=1
unset CFLAGS CPPFLAGS CXXFLAGS LDFLAGS CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH COMPILER_PATH GCC_EXEC_PREFIX
python3 - "$recipe_dir/source-lock.json" <<'PYTOOLS'
from pathlib import Path
import hashlib,json,sys
for path,expected in json.load(open(sys.argv[1]))['compiler_build_tools'].items():
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==expected, 'Toolchain changed; independent recipe review required: '+path
PYTOOLS
mkdir -p "$p/downloads" "$p/static-variant" "$output"
v="$p/static-variant"

fetch_git() {
    local url="$1" commit="$2" path="$3"
    git clone --filter=blob:none --no-checkout "$url" "$path"
    git -C "$path" fetch --depth 1 origin "$commit"
    git -C "$path" checkout --detach "$commit"
    [[ "$(git -C "$path" rev-parse HEAD)" == "$commit" ]]
    [[ -z "$(git -C "$path" status --porcelain --untracked-files=all)" ]] || {
        printf 'Refusing tracked or untracked source changes: %s\n' "$path" >&2; exit 1;
    }
}

download() {
    local file="$1" url="$2" expected="$3"
    if [[ ! -f "$file" ]] || ! printf '%s  %s\n' "$expected" "$file" | sha256sum -c - >/dev/null 2>&1; then
        curl --fail --location --retry 3 --output "$file.part" "$url"
        printf '%s  %s\n' "$expected" "$file.part" | sha256sum -c -
        mv "$file.part" "$file"
    fi
    printf '%s  %s\n' "$expected" "$file" | sha256sum -c -
}

while IFS=$'\t' read -r name url commit; do
    path="$p/$name"
    case "$name" in
        OpenH264) path="$p/openh264";;
        SDL_ttf) path="$v/SDL_ttf";;
        FreeType) path="$v/SDL_ttf/external/freetype";;
        HarfBuzz) path="$v/SDL_ttf/external/harfbuzz";;
    esac
    fetch_git "$url" "$commit" "$path"
done < <(python3 - "$recipe_dir/source-lock.json" <<'PY'
import json,sys
for name,item in json.load(open(sys.argv[1]))['git_sources'].items():
    print(name,item['url'],item['commit'],sep='\t')
PY
)
# Vetted dependencies are exact tracked gitlinks, not untracked exemptions.
[[ -z "$(git -C "$v/SDL_ttf" status --porcelain --untracked-files=all)" ]] || exit 1
while IFS=$'\t' read -r name url digest; do
    download "$p/downloads/$name" "$url" "$digest"
done < <(python3 - "$recipe_dir/source-lock.json" <<'PY'
import json,sys
lock=json.load(open(sys.argv[1]))
item=lock['SDL3_source_archive'];print(item['name'],item['url'],item['sha256'],sep='\t')
for item in lock['runtime_packages']:print(item['file'],item['url'],item['sha256'],sep='\t')
PY
)
tar -xzf "$p/downloads/SDL3-3.2.28.tar.gz" -C "$v"
mkdir -p "$p/runtime"
for name in openssl cjson uriparser; do
    tar --zstd -xf "$p/downloads/$name.pkg.tar.zst" -C "$p/runtime"
done
python3 - "$recipe_dir" "$p" <<'PY'
from pathlib import Path
import hashlib,json,subprocess,sys
recipe,source=map(Path,sys.argv[1:]);lock=json.loads((recipe/'source-lock.json').read_text())['owner_patch']
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(recipe/lock['path'])==lock['sha256']
for name,pin in lock['files'].items():
    assert sha(source/name)==pin['upstream_sha256'], 'Upstream source changed: '+name
subprocess.run(['patch','--fuzz=0','-p1','-i',str(recipe/lock['path'])],cwd=source,check=True)
for name,pin in lock['files'].items():
    assert sha(source/name)==pin['patched_sha256'], 'Patched source mismatch: '+name
PY
install -m 0644 "$recipe_dir/toolchain.cmake" "$v/toolchain.cmake"
meson_options=()
[[ ! -f "$p/openh264-build/meson-private/coredata.dat" ]] || meson_options=(--reconfigure)
path_maps=()
for kind in file macro debug; do
    path_maps+=("-f${kind}-prefix-map=$product_repo=/usr/src/uu-remote/product"
                "-f${kind}-prefix-map=$p=/usr/src/uu-remote/source")
done
CFLAGS="${path_maps[*]}" CXXFLAGS="${path_maps[*]}" meson setup "${meson_options[@]}" "$p/openh264-build" "$p/openh264" --cross-file "$recipe_dir/mingw.ini" \
    --prefix "$p/codec" --libdir lib --default-library static -Dbuildtype=release -Dtests=disabled \
    '-Dcpp_link_args=["-static-libgcc", "-static-libstdc++"]'
ninja -C "$p/openh264-build" -j 2
ninja -C "$p/openh264-build" -j 2 install
"$recipe_dir/build-sdl.sh"
"$recipe_dir/build-freerdp.sh"
python3 "$recipe_dir/strict-owner-unit.py" "$p"
install -m 0755 "$v/freerdp-build/client/SDL/SDL3/sdl-freerdp.exe" "$output/sdl-freerdp.exe"
install -m 0755 "$v/freerdp-build/libfreerdp/libfreerdp3.dll" \
    "$v/freerdp-build/client/common/libfreerdp-client3.dll" \
    "$v/freerdp-build/winpr/libwinpr/libwinpr3.dll" "$output/"
for name in libcrypto-3-x64.dll libssl-3-x64.dll libcjson-1.dll liburiparser-1.dll; do
    install -m 0755 "$p/runtime/mingw64/bin/$name" "$output/$name"
done
for name in libgcc_s_seh-1.dll libstdc++-6.dll libwinpthread-1.dll; do
    source="$(x86_64-w64-mingw32-g++-posix -print-file-name="$name")"
    [[ -f "$source" ]]
    install -m 0755 "$source" "$output/$name"
done
python3 - "$recipe_dir/source-lock.json" "$product_repo" "$output" <<'PYSHIM'
from pathlib import Path
import hashlib,json,subprocess,sys
lock=json.load(open(sys.argv[1]))['compat_shim'];repo,output=map(Path,sys.argv[2:])
source=repo/lock['source']
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(source)==lock['source_sha256'], 'SSPI shim source changed'
assert sha(repo/lock['build_recipe'])==lock['build_recipe_sha256'], 'Compat build recipe changed'
argv=[lock['compiler'],*lock['flags'],*[f'-f{kind}-prefix-map={repo}=/usr/src/uu-remote/product' for kind in ('file','macro','debug')],'-o','winpr-sspi-shim.dll',str(source)]
subprocess.run(argv,check=True,timeout=30,cwd=output)
assert sha(source)==lock['source_sha256'], 'SSPI shim source changed during compile'
PYSHIM
mkdir -p "$output/ossl-modules"
install -m 0755 "$p/runtime/mingw64/lib/ossl-modules/legacy.dll" "$output/ossl-modules/legacy.dll"
(
    cd "$recipe_dir"
    sha256sum bootstrap.sh build-sdl.sh build-freerdp.sh strict-owner-unit.py \
        toolchain.cmake lto-cap.cmake check-lto.py mingw.ini source-lock.json freerdp-sdl-owner-refresh.patch
) | sha256sum | awk '{print $1}' >"$output/.build-recipe.tmp"
(
    cd "$output"
    sha256sum sdl-freerdp.exe libfreerdp3.dll libfreerdp-client3.dll libwinpr3.dll \
        libcrypto-3-x64.dll libssl-3-x64.dll libcjson-1.dll liburiparser-1.dll \
        libgcc_s_seh-1.dll libstdc++-6.dll libwinpthread-1.dll winpr-sspi-shim.dll \
        ossl-modules/legacy.dll >.build-sha256.tmp
    mv .build-sha256.tmp .build-sha256
)
mv "$output/.build-recipe.tmp" "$output/.build-recipe"
printf 'Source build complete; checksums are build receipts, not release approval.\n'
