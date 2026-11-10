#!/bin/sh
# Bootstrap only a checksum-verified release on a new production host.
set -eu
umask 077

if [ "$(id -u)" -ne 0 ]; then
  echo "bootstrap must run as root" >&2
  exit 1
fi

incoming=${1:?incoming release directory is required}
case "$incoming" in
  /opt/zhiku/incoming/*) ;;
  *) echo "release must be under /opt/zhiku/incoming" >&2; exit 2 ;;
esac

version=${incoming##*/}
archive="$incoming/zhiku-$version.tar.gz"
checksum="$archive.sha256"
test -f "$archive"
test -f "$checksum"
(
  cd "$incoming"
  sha256sum -c "$(basename "$checksum")"
)

unpacked="$incoming/bootstrap-unpacked"
if [ -e "$unpacked" ]; then
  echo "bootstrap staging directory already exists" >&2
  exit 2
fi
mkdir -m 700 "$unpacked"
tar -xzf "$archive" -C "$unpacked"
(
  cd "$unpacked"
  sha256sum -c SHA256SUMS
)
test -f "$unpacked/deploy_release.py"
test -f "$unpacked/bin/deploy-release"
exec 9>/opt/zhiku/deploy.lock
if ! flock -n 9; then
  echo "another deployment is running" >&2
  exit 3
fi
exec /usr/bin/python3 "$unpacked/deploy_release.py" bootstrap "$unpacked"
