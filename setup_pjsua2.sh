#!/usr/bin/env bash
set -Eeuo pipefail

# Ubuntu/Linux setup for the PBX test caller.
# This intentionally does not install or configure WSL.

PJSIP_VERSION="${PJSIP_VERSION:-2.14.1}"
PJSUA_MAX_CALLS="${PJSUA_MAX_CALLS:-64}"
PJSUA_MAX_RECORDERS="${PJSUA_MAX_RECORDERS:-64}"
PJSUA_MAX_PLAYERS="${PJSUA_MAX_PLAYERS:-128}"
PJSUA_MAX_CONF_PORTS="${PJSUA_MAX_CONF_PORTS:-512}"
# Each call needs 2 I/O handles (RTP + RTCP), plus 1 for the SIP transport.
# PJSIP's default of 64 caps you at ~31 concurrent calls regardless of
# PJSUA_MAX_CALLS. Keep this below FD_SETSIZE (1024) when using select().
PJ_IOQUEUE_MAX_HANDLES="${PJ_IOQUEUE_MAX_HANDLES:-512}"
PJSUA2_USE_EPOLL="${PJSUA2_USE_EPOLL:-1}"
PJSUA2_CLEAN_BUILD="${PJSUA2_CLEAN_BUILD:-1}"
PJSUA2_FIX_VENV_OWNER="${PJSUA2_FIX_VENV_OWNER:-1}"
BUILD_ROOT="${PJSUA2_BUILD_ROOT:-$HOME/.cache/pjsua2-build}"
SRC_DIR="$BUILD_ROOT/pjproject-$PJSIP_VERSION"
TARBALL="$BUILD_ROOT/pjproject-$PJSIP_VERSION.tar.gz"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PJSUA2_VENV_DIR:-$PROJECT_DIR/venv}"

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
  sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get update
  sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y \
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

ensure_project_venv() {
  log "Preparing project virtualenv"

  if [[ -n "${VIRTUAL_ENV:-}" && "$VIRTUAL_ENV" != "$VENV_DIR" ]]; then
    printf 'Switching from active virtualenv %s to project virtualenv %s\n' "$VIRTUAL_ENV" "$VENV_DIR"
  fi

  if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
    printf 'Created virtualenv: %s\n' "$VENV_DIR"
  else
    printf 'Using existing virtualenv: %s\n' "$VENV_DIR"
  fi

  # Activate for the rest of this script. This does not persist after the script exits.
  # Run `source venv/bin/activate` manually if you want the parent shell activated too.
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"

  if ! python3 -c 'import os, sys, sysconfig; p=sysconfig.get_paths()["purelib"]; os.makedirs(p, exist_ok=True); sys.exit(0 if os.access(p, os.W_OK) else 1)'; then
    if [[ "$PJSUA2_FIX_VENV_OWNER" != "1" ]]; then
      die "Virtualenv is not writable by $(id -un): $VENV_DIR. Fix it with: sudo chown -R $(id -u):$(id -g) '$VENV_DIR'"
    fi

    if [[ "$VENV_DIR" != "$PROJECT_DIR"/venv && "$VENV_DIR" != "$PROJECT_DIR"/venv/* ]]; then
      die "Refusing to auto-fix ownership outside the project venv: $VENV_DIR"
    fi

    if [[ ! -f "$VENV_DIR/pyvenv.cfg" ]]; then
      die "Refusing to auto-fix ownership because this does not look like a Python venv: $VENV_DIR"
    fi

    printf 'Virtualenv is not writable by %s; fixing ownership with sudo.\n' "$(id -un)"
    sudo chown -R "$(id -u):$(id -g)" "$VENV_DIR"
    chmod -R u+rwX "$VENV_DIR"

    if ! python3 -c 'import os, sys, sysconfig; p=sysconfig.get_paths()["purelib"]; sys.exit(0 if os.access(p, os.W_OK) else 1)'; then
      die "Virtualenv is still not writable after ownership fix: $VENV_DIR"
    fi
  fi
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

/* I/O queue capacity. 1 handle for the SIP transport + 2 per call
 * (RTP and RTCP). The PJSIP default of 64 is what limits concurrency
 * to ~31 calls with PJ_ETOOMANY on media transport creation. */
#define PJ_IOQUEUE_MAX_HANDLES $PJ_IOQUEUE_MAX_HANDLES
#define PJ_IOQUEUE_MAX_EVENTS_IN_SINGLE_POLL $PJ_IOQUEUE_MAX_HANDLES
#define PJ_IOQUEUE_HAS_SAFE_UNREG 1

#endif
EOF

  printf 'Set PJSUA_MAX_CALLS=%s\n' "$PJSUA_MAX_CALLS"
  printf 'Set PJSUA_MAX_RECORDERS=%s\n' "$PJSUA_MAX_RECORDERS"
  printf 'Set PJSUA_MAX_PLAYERS=%s\n' "$PJSUA_MAX_PLAYERS"
  printf 'Set PJSUA_MAX_CONF_PORTS=%s\n' "$PJSUA_MAX_CONF_PORTS"
  printf 'Set PJ_IOQUEUE_MAX_HANDLES=%s\n' "$PJ_IOQUEUE_MAX_HANDLES"
  printf 'Wrote compile config: %s\n' "$config_site"

  local max_supported_calls=$(( (PJ_IOQUEUE_MAX_HANDLES - 1) / 2 ))
  printf 'Concurrency ceiling from these settings: %s calls (min of PJSUA_MAX_CALLS=%s and ioqueue=%s)\n' \
    "$(( PJSUA_MAX_CALLS < max_supported_calls ? PJSUA_MAX_CALLS : max_supported_calls ))" \
    "$PJSUA_MAX_CALLS" "$max_supported_calls"
}

build_pjsua2() {
  log "Configuring and building PJSIP/PJSUA2"
  cd "$SRC_DIR"

  if [[ "$PJSUA2_CLEAN_BUILD" == "1" && -f Makefile ]]; then
    log "Cleaning previous PJSIP build output"
    make clean || true
    # config_site.h is included by nearly every source file. A stale object
    # file silently keeps the OLD limits even after the header changes, which
    # is the usual reason a rebuild appears to do nothing.
    find "$SRC_DIR" -name '*.o' -delete 2>/dev/null || true
    find "$SRC_DIR" -name '*.a' -delete 2>/dev/null || true
  elif [[ -f Makefile ]]; then
    log "WARNING: skipping clean. Limit changes in config_site.h may NOT take effect."
  fi

  local configure_args=(CFLAGS="-fPIC -O2")
  if [[ "$PJSUA2_USE_EPOLL" == "1" ]]; then
    log "Enabling epoll I/O backend (scales better than select at high call counts)"
    configure_args=(--enable-epoll "${configure_args[@]}")
  fi

  if ! ./configure "${configure_args[@]}"; then
    if [[ "$PJSUA2_USE_EPOLL" == "1" ]]; then
      log "configure failed with --enable-epoll; retrying with the default backend"
      ./configure CFLAGS="-fPIC -O2"
    else
      die "configure failed"
    fi
  fi

  make dep
  make -j"$(nproc)"

  # Pin the linker to the tree we just built. /usr/local/lib is on the default
  # search path, so an older pjsip install there wins otherwise - producing a
  # binding linked against stale libraries carrying stale compile-time limits.
  # This is silent: the build succeeds and the new config_site.h is ignored.
  export LDFLAGS="-L$SRC_DIR/pjsip/lib -L$SRC_DIR/pjlib/lib -L$SRC_DIR/pjlib-util/lib -L$SRC_DIR/pjmedia/lib -L$SRC_DIR/pjnath/lib -L$SRC_DIR/third_party/lib${LDFLAGS:+ $LDFLAGS}"
  log "Pinned LDFLAGS to the freshly built libraries"

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

  local imported_from expected_prefix
  imported_from="$(python3 -c 'import pjsua2; print(pjsua2.__file__)')"
  expected_prefix="$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  printf 'pjsua2 imported from: %s\n' "$imported_from"

  if [[ "$imported_from" != "$expected_prefix"* ]]; then
    printf '\nWARNING: pjsua2 is being imported from outside this virtualenv.\n'
    printf 'Another install is shadowing the one just built, so rebuilds will\n'
    printf 'appear to have no effect. Remove the other copy, then re-run.\n\n'
  fi

  printf 'Built config_site.h:\n'
  sed -n '/#define/p' "$SRC_DIR/pjlib/include/pj/config_site.h" | sed 's/^/  /'
}

check_project_files() {
  log "Checking project runtime files"
  cd "$PROJECT_DIR"

  if [[ ! -f ".env" ]]; then
    printf 'No .env found. Create it manually before running pjsip_test_call.py.\n'
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
  ensure_project_venv
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