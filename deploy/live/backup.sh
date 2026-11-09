#!/usr/bin/env bash
# One consistent backup of the live stack: the PostgreSQL database (pg_dump, custom format) plus a copy of deploy/live/.env
# (it holds LINKEDEYE_CREDENTIAL_KEY: without it the stored SSH/SNMP/Prometheus credentials in the dump cannot be read), in
# one tar archive with a checksum manifest. Uploads are kept in the database, so there is no separate media directory.
#
#   ./deploy/live/backup.sh                    # -> deploy/live/backups/linkedeye-<UTC timestamp>.tar
#   ./deploy/live/backup.sh --dir /mnt/backup --keep-days 30
#   ./deploy/live/backup.sh --with-caddy       # also the TLS certificates / ACME account (avoids re-issuing after a rebuild)
#
# Retention: archives older than LINKEDEYE_BACKUP_KEEP_DAYS (default 14) are deleted, but the newest 3 are always kept.
# Run it from cron (docs/GO_LIVE.md) and copy the archives off this host. The archive is mode 600: it contains secrets.
set -euo pipefail
# shellcheck source=deploy/live/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DIR_ARG=""; KEEP_ARG=""; WITH_CADDY=0; QUIET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR_ARG="${2:?--dir needs a path}"; shift 2 ;;
    --keep-days) KEEP_ARG="${2:?--keep-days needs a number}"; shift 2 ;;
    --with-caddy) WITH_CADDY=1; shift ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done
chat() { [ "$QUIET" -eq 1 ] || say "$@"; }

need_cmd docker "Install Docker."
need_cmd tar "Install tar."
[ -f "$ENV_FILE" ] || die "$ENV_FILE not found: nothing to back up (run bootstrap.sh first)."

backup_dir="${DIR_ARG:-$(get_env LINKEDEYE_BACKUP_DIR)}"; backup_dir="${backup_dir:-$LIVE_DIR/backups}"
keep_days="${KEEP_ARG:-$(get_env LINKEDEYE_BACKUP_KEEP_DAYS 14)}"
[[ "$keep_days" =~ ^[0-9]+$ ]] || die "--keep-days must be a whole number."
umask 077
mkdir -p "$backup_dir"
chmod 700 "$backup_dir" 2>/dev/null || true

# two overlapping runs (cron + a manual one) would write the same files
lock="$backup_dir/.backup.lock"
if ! mkdir "$lock" 2>/dev/null; then
  if [ -n "$(find "$lock" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then rm -rf "$lock"; mkdir "$lock"
  else die "another backup is running ($lock). If none is, remove that directory."; fi
fi

stamp="$(timestamp)"
work="$(mktemp -d "$backup_dir/.work-$stamp.XXXXXX")"
trap 'rm -rf "$work" "$lock"' EXIT

state="$(service_state db)"
[ "$state" = healthy ] || die "the db service is '$state', not healthy. Start the stack: docker compose -f deploy/live/docker-compose.live.yml up -d"

chat "Dumping the database ..."
dc exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$work/db.dump" || die "pg_dump failed."
[ -s "$work/db.dump" ] || die "pg_dump produced an empty file."
dc exec -T db pg_restore --list < "$work/db.dump" > /dev/null || die "the dump cannot be read back by pg_restore; not keeping it."

cp "$ENV_FILE" "$work/env"
revision="$(git -C "$LIVE_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
image="$(get_env LINKEDEYE_IMAGE linkedeye:9.0)"
{
  echo "created_utc=$stamp"
  echo "git_revision=$revision"
  echo "image=$image"
  echo "domain=$(get_env LINKEDEYE_DOMAIN)"
  echo "postgres=$(dc exec -T db postgres --version 2>/dev/null | tr -d '\r' || echo unknown)"
} > "$work/MANIFEST"

if [ "$WITH_CADDY" -eq 1 ]; then
  chat "Archiving the Caddy data volume (certificates) ..."
  vol="$(docker volume ls -q | grep -E '(^|_)caddy-data$' | head -n 1 || true)"
  if [ -n "$vol" ]; then
    docker run --rm --entrypoint tar -v "$vol:/data:ro" -v "$work:/out" "$(get_env LINKEDEYE_CADDY_IMAGE caddy:2.10.0-alpine)" \
      czf /out/caddy-data.tgz -C /data . || warn "could not archive the Caddy volume; continuing without it."
  else
    warn "no caddy-data volume found; continuing without it."
  fi
fi

( cd "$work" && sha256sum -- * > SHA256SUMS ) 2>/dev/null || ( cd "$work" && shasum -a 256 -- * > SHA256SUMS )
final="$backup_dir/linkedeye-$stamp.tar"
tar -C "$work" -cf "$final.partial" .
mv "$final.partial" "$final"
chmod 600 "$final"

# retention: delete archives older than keep_days, but never the newest three
kept=0
while IFS= read -r f; do
  kept=$((kept + 1))
  [ "$kept" -gt 3 ] || continue
  if [ -n "$(find "$f" -maxdepth 0 -mtime +"$keep_days" 2>/dev/null)" ]; then rm -f -- "$f"; chat "removed old backup $(basename "$f")"; fi
done < <(ls -1 "$backup_dir"/linkedeye-*.tar 2>/dev/null | sort -r)
find "$backup_dir" -maxdepth 1 -name 'linkedeye-*.tar.partial' -mmin +360 -delete 2>/dev/null || true

size="$(du -h "$final" | cut -f1)"
if [ "$QUIET" -eq 1 ]; then printf '%s\n' "$final"; else ok "backup written: $final ($size)"; note "copy it off this host; restore with ./deploy/live/restore.sh $final"; fi
