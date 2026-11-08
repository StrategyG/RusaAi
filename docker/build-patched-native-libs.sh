#!/bin/sh
# Build checksum-pinned, patched Expat and SQLite, then rebuild CPython against
# those system libraries. The official Python slim images bundle pyexpat unless
# CPython is explicitly configured with --with-system-expat.
set -eu

build_root="$(mktemp -d)"
trap 'rm -rf "$build_root"' EXIT HUP INT TERM

cd "$build_root"

download_flags="--retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 20 --max-time 180 --fail --silent --show-error --location"

expat_archive="expat-2.10.0.tar.xz"
curl $download_flags \
  "https://github.com/libexpat/libexpat/releases/download/R_2_10_0/$expat_archive" \
  --output "$expat_archive"
echo "c624ba2998fc65df1163a49641ff3f9ab73138b11641d92c86bee0fb01b1bc7f  $expat_archive" \
  | sha256sum --check --status

tar -xJf "$expat_archive"
cd "$build_root/expat-2.10.0"
./configure --prefix=/usr/local --disable-static --enable-shared
make -j"$(getconf _NPROCESSORS_ONLN)"
make install

cd "$build_root"
sqlite_archive="sqlite-autoconf-3540100.tar.gz"
curl $download_flags \
  "https://www.sqlite.org/2026/$sqlite_archive" \
  --output "$sqlite_archive"
python - "$sqlite_archive" <<'PY'
import hashlib
import sys
from pathlib import Path

expected = "b6ced585c750916b1052667239a12c25a7ff34e7f06e1e6e01ecb0427657d9c4"
actual = hashlib.sha3_256(Path(sys.argv[1]).read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit(f"SQLite source checksum mismatch: {actual}")
PY

tar -xzf "$sqlite_archive"
cd "$build_root/sqlite-autoconf-3540100"
CFLAGS="-O2 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_SESSION -DSQLITE_ENABLE_PREUPDATE_HOOK" \
  ./configure --prefix=/usr/local --disable-static --enable-shared \
    --enable-fts5 --enable-rtree --enable-session
make -j"$(getconf _NPROCESSORS_ONLN)"
make install

# Rebuild the exact CPython version already present in the builder image.
# This ensures pyexpat is dynamically linked to patched Expat rather than the
# bundled Expat copy shipped by the official Python image.
cd "$build_root"
python_version="$(python -c 'import platform; print(platform.python_version())')"
case "$python_version" in
  3.12.16)
    python_sha256="b5796915dc45ff745cfb4e990a7e7c93d6f546328a102ce31448d69ebc37575c"
    ;;
  3.11.18)
    python_sha256="9120e0a653868f6e3698d36cdd99e5a58903ec9469e448f21d98f7b16d8fa290"
    ;;
  *)
    echo "Unsupported Python source version for verified rebuild: $python_version" >&2
    exit 1
    ;;
esac

python_archive="Python-$python_version.tar.xz"
curl $download_flags \
  "https://www.python.org/ftp/python/$python_version/$python_archive" \
  --output "$python_archive"
echo "$python_sha256  $python_archive" | sha256sum --check --status
tar -xJf "$python_archive"
cd "$build_root/Python-$python_version"

export PKG_CONFIG_PATH="/usr/local/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export CPPFLAGS="-I/usr/local/include ${CPPFLAGS:-}"
export LDFLAGS="-L/usr/local/lib -Wl,-rpath,/usr/local/lib ${LDFLAGS:-}"
./configure --prefix=/usr/local --enable-shared \
  --enable-loadable-sqlite-extensions --with-system-expat \
  --with-ensurepip=install --without-static-libpython
make -j"$(getconf _NPROCESSORS_ONLN)"
make install

ldconfig
python - <<'PY'
import ctypes
import platform
import pyexpat
import sqlite3
import subprocess
import sys
import _sqlite3

expected_python = {(3, 12, 16), (3, 11, 18)}
if sys.version_info[:3] not in expected_python:
    raise SystemExit(f"Unexpected rebuilt Python version: {platform.python_version()}")

expat = ctypes.CDLL("libexpat.so.1")
expat.XML_ExpatVersion.restype = ctypes.c_char_p
expat_version = expat.XML_ExpatVersion().decode("ascii")

def linked_library(module, expected):
    result = subprocess.run(
        ["ldd", module.__file__], check=True, capture_output=True, text=True
    )
    if expected not in result.stdout:
        raise SystemExit(
            f"{module.__name__} is not using {expected}:\n{result.stdout}"
        )

if expat_version != "expat_2.10.0" or pyexpat.EXPAT_VERSION != expat_version:
    raise SystemExit(
        f"Expected pyexpat and shared Expat 2.10.0, got "
        f"{pyexpat.EXPAT_VERSION} / {expat_version}"
    )
if sqlite3.sqlite_version_info < (3, 54, 1):
    raise SystemExit(f"Expected SQLite >=3.54.1, got {sqlite3.sqlite_version}")
linked_library(pyexpat, "/usr/local/lib/libexpat.so.1")
linked_library(_sqlite3, "/usr/local/lib/libsqlite3.so.0")
db = sqlite3.connect(":memory:")
db.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(content)")
db.close()
print(
    f"Verified Python {platform.python_version()}, Expat {expat_version}, "
    f"SQLite {sqlite3.sqlite_version} with FTS5"
)
PY

# Runtime images need shared libraries and Python, not compiler headers or CLIs.
rm -f /usr/local/bin/xmlwf /usr/local/bin/sqlite3
rm -f /usr/local/include/expat.h /usr/local/include/expat_external.h
rm -f /usr/local/include/expat_config.h
rm -f /usr/local/include/sqlite3.h /usr/local/include/sqlite3ext.h
rm -f /usr/local/lib/pkgconfig/expat.pc /usr/local/lib/pkgconfig/sqlite3.pc
rm -rf /usr/local/share/doc/expat /usr/local/share/man/man1/xmlwf.1
ldconfig
