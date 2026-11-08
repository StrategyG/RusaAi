#!/bin/sh
# Build patched upstream Expat and SQLite into /usr/local for Debian Trixie
# runtime images while stable-distribution security backports are pending.
set -eu

build_root="$(mktemp -d)"
trap 'rm -rf "$build_root"' EXIT HUP INT TERM

cd "$build_root"

expat_archive="expat-2.10.0.tar.xz"
curl --fail --silent --show-error --location \
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
curl --fail --silent --show-error --location \
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

# Runtime containers need the shared libraries, not upstream CLI tools or headers.
rm -f /usr/local/bin/xmlwf /usr/local/bin/sqlite3
rm -f /usr/local/include/expat.h /usr/local/include/expat_external.h
rm -f /usr/local/include/sqlite3.h /usr/local/include/sqlite3ext.h
rm -f /usr/local/lib/pkgconfig/expat.pc /usr/local/lib/pkgconfig/sqlite3.pc
rm -rf /usr/local/share/doc/expat /usr/local/share/man/man1/xmlwf.1

ldconfig
LD_LIBRARY_PATH=/usr/local/lib python - <<'PY'
import pyexpat
import sqlite3

if pyexpat.EXPAT_VERSION != "expat_2.10.0":
    raise SystemExit(f"Expected patched Expat 2.10.0, got {pyexpat.EXPAT_VERSION}")
if sqlite3.sqlite_version_info < (3, 54, 1):
    raise SystemExit(f"Expected patched SQLite >=3.54.1, got {sqlite3.sqlite_version}")
print(f"Patched native parsers verified: {pyexpat.EXPAT_VERSION}; SQLite {sqlite3.sqlite_version}")
PY
