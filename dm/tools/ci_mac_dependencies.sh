#!/bin/bash
# Dependency setup on a disposable ARM64 Actions Mac, shared by sharded jobs.
set -euo pipefail
[[ "$(uname -m)" == arm64 ]] || { echo '[RED] An ARM64 Mac is required.'; exit 1; }
if [[ "${RUNNER_ENVIRONMENT:-}" == github-hosted ]]; then
  active_developer_dir="${DEVELOPER_DIR:-$(xcode-select -p)}"
  active_xcode="$(cd "$active_developer_dir/../.." && pwd -P)"
  for candidate in /Applications/Xcode_*.app; do
    [[ -d "$candidate" && ! -L "$candidate" ]] || continue
    if [[ "$(cd "$candidate" && pwd -P)" != "$active_xcode" ]]; then sudo rm -rf -- "$candidate"; fi
  done
fi
case "${1:-}" in
  prepare)
    brew install xcodegen zstd spirv-cross glslang gnupg
    python -m pip install lz4 capstone fonttools 'backports.zstd; python_version < "3.14"'
    ;;
  compile)
    brew install spirv-cross gnupg
    if ! xcrun --sdk iphoneos metal --version; then xcodebuild -downloadComponent MetalToolchain; fi
    xcrun --sdk iphoneos metal --version
    xcrun --sdk macosx metal --version
    ;;
  assemble)
    brew install xcodegen gnupg
    python -m pip install fonttools
    ;;
  *) echo '[RED] Expected prepare, compile or assemble.'; exit 1 ;;
esac
xcodebuild -version
df -h "$RUNNER_TEMP"
