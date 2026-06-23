#!/usr/bin/env bash
set -Eeuo pipefail

# Ubuntu/Linux setup for the PBX test caller.
# This intentionally does not install or configure WSL.

PJSIP_VERSION="${PJSIP_VERSION:-2.14.1}"
PJSUA_MAX_CALLS="${PJSUA_MAX_CALLS:-64}"
PJSUA_MAX_RECORDERS="${PJSUA_MAX_RECORDERS:-64}"
PJSUA_MAX_PLAYERS="${PJSUA_MAX_PLAYERS:-128}"
PJSUA_MAX_CONF_PORTS="${PJSUA_MAX_CONF_PORTS:-256}"
BUILD_ROOT="${PJSUA2_BUILD_ROOT:-$HOME/.cache/pjsua2-build}"
SRC_DIR="$BUILD_ROOT/pjproject-$PJSIP_VERSION"
TARBALL="$BUILD_ROOT/pjproject-$PJSIP_VERSION.tar.gz"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() {
  printf '\n==> %s\n' "$*"
}

die() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

need_command() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

pip_install() {
  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    python3 -m pip install "$@"
  else
    python3 -m pip install --user --break-system-packages "$@" \
      || python3 -m pip install --user "$@"
  fi
}

install_apt_packages() {
  if ! command -v apt-get >/dev/null 2>&1; then
    die "This installer expects Ubuntu/Debian with apt-get. Run it inside an Ubuntu/Linux terminal."
  fi

  log "Installing build dependencies"
  sudo apt-get update
  sudo apt-get install -y \
    build-essential \
    ca-certificates \
    libasound2-dev \
    pkg-config \
    python3-dev \
    python3-pip \
    python3-setuptools \
    python3-wheel \
    python3-venv \
    swig \
    tar \
    wget
}

prepare_python_build_env() {
  log "Preparing Python build environment"

  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    printf 'Using active virtualenv: %s\n' "$VIRTUAL_ENV"
  else
    printf 'Using system Python with user site-packages.\n'
  fi

  pip_install setuptools wheel

  # PJSIP 2.14.1 imports distutils from setup.py. Python 3.12+ removed
  # stdlib distutils, so use setuptools' compatible implementation.
  export SETUPTOOLS_USE_DISTUTILS=local

  python3 -c "import distutils.core; print('distutils OK')"
}

patch_swig_python_build() {
  log "Patching SWIG Python build for current Ubuntu/SWIG versions"

  local swig_dir="$SRC_DIR/pjsip-apps/src/swig"
  cd "$swig_dir"

  # PJSIP 2.14.1 adds this SWIG flag, but newer SWIG-generated Python map
  # wrappers can then reference iterator templates that were not emitted.
  # Removing it fixes errors around SwigPyMapIterator_T / SwigPyIteratorClosed_T.
  find "$swig_dir" -type f \( -name 'Makefile' -o -name '*.mak' \) \
    -exec sed -i 's/[[:space:]]*-DSWIG_NO_EXPORT_ITERATOR_METHODS//g' {} +

  # Force regeneration if a previous run produced a broken wrapper.
  rm -f "$swig_dir/pjsua2_wrap.cpp"
  rm -rf "$swig_dir/python/build"
}

download_pjsip() {
  mkdir -p "$BUILD_ROOT"

  if [[ ! -f "$TARBALL" ]]; then
    log "Downloading PJSIP $PJSIP_VERSION"
    wget -O "$TARBALL" "https://github.com/pjsip/pjproject/archive/refs/tags/$PJSIP_VERSION.tar.gz"
  else
    log "Using existing download: $TARBALL"
  fi

  if [[ ! -d "$SRC_DIR" ]]; then
    log "Extracting PJSIP source"
    tar -xzf "$TARBALL" -C "$BUILD_ROOT"
  else
    log "Using existing source tree: $SRC_DIR"
  fi
}

write_pjsip_config_site() {
  log "Configuring PJSIP compile-time call/media capacity"

  local config_site="$SRC_DIR/pjlib/include/pj/config_site.h"

  cat > "$config_site" <<EOF
#ifndef PJ_CONFIG_SITE_H
#define PJ_CONFIG_SITE_H

#define PJSUA_MAX_CALLS $PJSUA_MAX_CALLS
#define PJSUA_MAX_RECORDERS $PJSUA_MAX_RECORDERS
#define PJSUA_MAX_PLAYERS $PJSUA_MAX_PLAYERS
#define PJSUA_MAX_CONF_PORTS $PJSUA_MAX_CONF_PORTS

#endif
EOF

  printf 'Set PJSUA_MAX_CALLS=%s\n' "$PJSUA_MAX_CALLS"
  printf 'Set PJSUA_MAX_RECORDERS=%s\n' "$PJSUA_MAX_RECORDERS"
  printf 'Set PJSUA_MAX_PLAYERS=%s\n' "$PJSUA_MAX_PLAYERS"
  printf 'Set PJSUA_MAX_CONF_PORTS=%s\n' "$PJSUA_MAX_CONF_PORTS"
  printf 'Wrote compile config: %s\n' "$config_site"
}

build_pjsua2() {
  log "Configuring and building PJSIP/PJSUA2"
  cd "$SRC_DIR"

  if [[ -f Makefile ]]; then
    log "Cleaning previous PJSIP build output"
    make clean || true
  fi

  ./configure CFLAGS="-fPIC -O2"
  make dep
  make -j"$(nproc)"

  log "Building Python pjsua2 bindings"
  patch_swig_python_build
  cd "$SRC_DIR/pjsip-apps/src/swig"
  make python
}

install_python_module() {
  log "Installing pjsua2 into the active Python site-packages"

  local site_packages build_dir
  site_packages="$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  mkdir -p "$site_packages"

  build_dir="$(
    find "$SRC_DIR/pjsip-apps/src/swig/python/build" \
      -type f \
      -name pjsua2.py \
      -path '*build*lib.*' \
      -print \
      -quit 2>/dev/null | xargs -r dirname
  )"

  if [[ -z "$build_dir" ]]; then
    die "Could not find built pjsua2.py under $SRC_DIR/pjsip-apps/src/swig/python/build"
  fi

  cp "$build_dir/pjsua2.py" "$build_dir"/_pjsua2*.so "$site_packages/"
  printf 'Copied pjsua2 files to: %s\n' "$site_packages"
}

install_python_dependencies() {
  log "Installing Python helper dependency: python-dotenv"
  pip_install python-dotenv
}

verify_imports() {
  log "Verifying Python imports"
  python3 -c "import pjsua2; print('pjsua2 OK')"
  python3 -c "import dotenv; print('dotenv OK')"
}

check_project_files() {
  log "Checking project runtime files"
  cd "$PROJECT_DIR"

  if [[ ! -f ".env" ]]; then
    cat > .env.example <<'ENV'
# Copy this to .env and fill in the real PBX values.
ASTERISK_HOST=10.29.32.138
REMOTE_SIP_PORT=5060
LOCAL_SIP_PORT=5062
MEDIA_RTP_PORT=4000
MEDIA_RTP_PORT_RANGE=400

CALLER_USER=1001
CALLER_PASS=
CALLER_DISPLAY=Rahul

DEST_NUMBER=19073750302
INPUT_AUDIO_DIR=input_audios
ENV
    printf 'No .env found. Created .env.example; copy it to .env and fill in the real values.\n'
  else
    printf '.env found.\n'
  fi

  local audio_dir missing=0
  audio_dir="${INPUT_AUDIO_DIR:-input_audios}"

  for wav in first.wav name2.wav birthday2.wav yes.wav no.wav height.wav weight.wav silence_60s.wav; do
    if [[ ! -f "$audio_dir/$wav" ]]; then
      printf 'Missing audio file: %s/%s\n' "$audio_dir" "$wav"
      missing=1
    fi
  done

  if [[ "$missing" -eq 0 ]]; then
    printf 'Required audio files found in %s.\n' "$audio_dir"
  else
    printf 'Add the missing WAV files before running pjsip_test_call.py.\n'
  fi
}

main() {
  need_command python3
  install_apt_packages
  prepare_python_build_env
  download_pjsip
  write_pjsip_config_site
  build_pjsua2
  install_python_module
  install_python_dependencies
  verify_imports
  check_project_files

  log "Setup complete"
  printf 'Next step:\n'
  printf '  cd "%s"\n' "$PROJECT_DIR"
  printf '  python3 pjsip_test_call.py\n'
}

main "$@"
