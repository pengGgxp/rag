#!/bin/sh
# Root-only server entry point. Keep this wrapper small: the tested logic lives
# in deploy_release.py so CI can exercise the rollback decisions.
set -eu
umask 077

if [ "$(id -u)" -ne 0 ]; then
  echo "deploy-release must run as root" >&2
  exit 1
fi

incoming=${1:?incoming release directory is required}
case "$incoming" in
  /opt/zhiku/incoming/*) ;;
  *) echo "release path must be under /opt/zhiku/incoming" >&2; exit 2 ;;
esac

action=${2:-deploy}
case "$action" in
  deploy|shadow|stage|takeover) ;;
  *) echo "unknown release action" >&2; exit 2 ;;
esac

version=${incoming##*/}
archive="$incoming/zhiku-$version.tar.gz"
archive_sum="$archive.sha256"
if [ ! -f "$archive" ] || [ ! -f "$archive_sum" ]; then
  echo "release archive or checksum is missing" >&2
  exit 2
fi
(
  cd "$incoming"
  sha256sum -c "$(basename "$archive_sum")"
)
# Never unpack over the uploaded archive. A staging directory keeps extraction
# atomic enough for the checksum gate below and avoids stale files from a
# previous attempt.
staging="$incoming/unpacked"
if [ -e "$staging" ]; then
  echo "release staging directory already exists; use a new incoming directory" >&2
  exit 2
fi
unpacked="$staging/$version"
install -d -m 700 "$unpacked"
tar -xzf "$archive" -C "$unpacked"
if [ ! -f "$unpacked/release.json" ] || [ ! -f "$unpacked/SHA256SUMS" ]; then
  echo "release package did not unpack cleanly" >&2
  exit 2
fi
(
  cd "$unpacked"
  sha256sum -c SHA256SUMS
)
if [ ! -f "$unpacked/run_release.sh" ] || [ ! -f "$unpacked/deploy_release.py" ]; then
  echo "release runner is missing after unpacking" >&2
  exit 2
fi
chmod 700 "$unpacked/run_release.sh"
incoming="$unpacked"

# Never execute a deploy script that is still being written by the upload.
if ! grep -Eq '[[:space:]]+\.?/?release\.json$' "$incoming/SHA256SUMS"; then
  echo "release manifest is not covered by checksums" >&2
  exit 2
fi

exec 9>/opt/zhiku/deploy.lock
if ! flock -n 9; then
  echo "another deployment is running" >&2
  exit 3
fi

# Use the checked-in release package copied to the immutable incoming
# directory. During first takeover there is no trusted current release yet.
/usr/bin/python3 "$incoming/deploy_release.py" "$action" "$incoming"
