#!/usr/bin/env bash
set -Eeuo pipefail
v="${UURB_SOURCE_BUILD_ROOT:?set source build root}/static-variant"
cmake -S "$v/SDL3-3.2.28" -B "$v/sdl-build" -G Ninja -DCMAKE_TOOLCHAIN_FILE="$v/toolchain.cmake" -DCMAKE_INSTALL_PREFIX="$v/install" -DCMAKE_BUILD_TYPE=Release -DSDL_SHARED=OFF -DSDL_STATIC=ON -DSDL_TESTS=OFF -DSDL_TEST_LIBRARY=OFF -DSDL_EXAMPLES=OFF
python3 "$UURB_BUILD_RECIPE_DIR/check-lto.py" "$v/sdl-build"
ninja -C "$v/sdl-build" -j 2
ninja -C "$v/sdl-build" -j 2 install
for spec in 'freetype:9973564cfa63763a3e4ac67c09147899539b1e07' 'harfbuzz:564bf9818a18709776856533829c0c04950773d6'; do
 name="${spec%%:*}"; expected="${spec#*:}"
 actual="$(git -C "$v/SDL_ttf/external/$name" rev-parse HEAD)"
 [[ "$actual" == "$expected" ]]
done
cmake -S "$v/SDL_ttf" -B "$v/ttf-build" -G Ninja -DCMAKE_TOOLCHAIN_FILE="$v/toolchain.cmake" -DCMAKE_INSTALL_PREFIX="$v/install" -DCMAKE_PREFIX_PATH="$v/install" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DSDLTTF_VENDORED=ON -DSDLTTF_PLUTOSVG=OFF -DSDLTTF_HARFBUZZ=ON -DSDLTTF_SAMPLES=OFF -DSDLTTF_INSTALL_MAN=OFF
python3 "$UURB_BUILD_RECIPE_DIR/check-lto.py" "$v/ttf-build"
ninja -C "$v/ttf-build" -j 2
ninja -C "$v/ttf-build" -j 2 install
