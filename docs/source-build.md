[English](source-build.md) · العربية · Deutsch · Español · Français · 日本語 · 한국어 · Русский · Tiếng Việt · 简体中文 · 繁體中文
Home
# Building and reusing the desktop relay
The installer builds a pinned FreeRDP SDL client with Plus's clipboard and desktop-size patches. [The recipe](../vendor/freerdp-sdl-build/), [source lock](../vendor/freerdp-sdl-build/source-lock.json) and [product profile](../patches/freerdp-sdl-product.json) record the revisions, archives, toolchain and 13 Windows runtime files.
<a id="reference-toolchain"></a>
## Prepare the Ubuntu 26.04 reference tools
On Ubuntu 26.04 amd64, the [preparation script](../scripts/prepare-build-toolchain.py) downloads 17 fixed official Ubuntu packages and verifies 14 compiler/build-tool files against `source-lock.json`. The [package manifest](../patches/reference-build-packages.json) records package versions, sizes and hashes. The script downloads, privately extracts and checks these files; it does not install system packages. Run from the repository root:
```bash
python3 scripts/prepare-build-toolchain.py \
  --packages-dir "$HOME/.cache/uu-plus/reference-packages" \
  --root "$HOME/.cache/uu-plus/reference-root"
```
`--packages-dir` selects the package cache; `--root` must be a new or empty private directory. To repeat extraction and tool checks from the cached packages without network access, choose another empty root and add `--verify-only`:
```bash
python3 scripts/prepare-build-toolchain.py \
  --packages-dir "$HOME/.cache/uu-plus/reference-packages" \
  --root "$HOME/.cache/uu-plus/reference-root-offline" \
  --verify-only
```
The script prints a `sudo apt install` command containing all 17 local `.deb` paths. On Ubuntu 26.04, run that command manually to install the reference tools. The existing build recipe reads fixed `/usr` paths; the private extraction root is for checking the tools, not selecting the build environment.
Then use the normal installer. It prepares the remaining host build dependencies, WineHQ and GNOME runtime packages, runs the existing relay build and validates the runtime before deployment. Tool preparation covers the 14 reference files; the full source build and its 13 runtime files are checked by this flow:
```bash
./install.sh
./scripts/verify.sh --quick
```
For Ubuntu 24.04, use an existing verified output matching the current product profile through the reuse flow below. The Ubuntu 26.04 packages above are specific to 26.04.
## Build and reuse
A cold source build requires the exact reviewed compiler and build-tool files recorded in `source-lock.json`. Ordinary distribution APT packages do not automatically provide that toolchain.
A normal `./install.sh` prepares packages and validates the runtime before replacing the installed relay. It reuses `build/freerdp` only when provenance matches the current product profile, recipe and runtime pins. Otherwise `scripts/build-winpr.sh` builds fresh sources with two jobs and a 900-second deadline, then checks the result.
With the reviewed tools installed at the recipe's `/usr` paths and the host build dependencies ready, you can also build a separate relay cache:
```bash
UURB_BUILD_DIR=/absolute/path/to/build-work   ./scripts/build-winpr.sh /absolute/path/to/fresh-output
```
Choose a new output directory. A unique source job is created under the build-work directory, independently of any running Wine prefix. To reuse a verified output during an update:
From the repository root, first validate an existing output against the current profile. The normal installer prepares host dependencies. Use `--skip-packages` only when host compatibility build tools, WineHQ and GNOME runtime packages are already installed; `--skip-account-login` is optional for an existing configured account.
```bash
python3 scripts/verify-freerdp-runtime.py --mode build /absolute/path/to/verified/output
```
Then reuse that output through the existing installer entry:
```bash
UURB_FREERDP_PREBUILT_DIR=/absolute/path/to/verified/output   ./install.sh
```
The cache must match the current recipe, runtime pins and product-profile provenance. A changed profile requires the build and validation entry to run again; editing a generated receipt or checksum cannot approve different binaries. The installer checks the copied runtime before starting the service.
## Fixed build inputs
FreeRDP/WinPR, SDL 3.2.28, SDL_ttf, OpenH264, FreeType and HarfBuzz are built from fixed inputs, alongside pinned OpenSSL, cJSON and uriparser runtime packages. Source patches and MinGW/compiler/build-tool hashes are checked before compilation. Changes to tools or dependencies require a reviewed recipe.
File, macro and debug prefix maps normalize source and output roots in binaries. FreeRDP opaque settings are explicitly OFF from the first configuration; the compatibility DLL uses a relative output name. This keeps local checkout paths out of the runtime.
## Build results
The pinned toolchain produced identical bytes for all 13 runtime files in two different source/output roots. Initial and repeated CMake configuration produced the same metadata. A complete clean-source run without prebuilt output took about 700 seconds, within the 900-second deadline, and passed runtime and provenance checks.
These are build results. Canvas and streaming settings are described in the quality guide; build resource observations are in performance and costs.
