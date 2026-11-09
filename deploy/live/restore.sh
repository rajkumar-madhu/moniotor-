#!/usr/bin/env bash
# Restore a backup made by backup.sh into the live stack. DESTRUCTIVE: the database is replaced by the one in the archive.
#
#   ./deploy/live/restore.sh deploy/live/backups/linkedeye-20261109T020000Z.tar
#   ./deploy/live/restore.sh ARCHIVE --restore-env     # also bring back the archived .env (new server, lost .env)
#   ./deploy/live/restore.sh ARCHIVE --yes             # do not ask
#
# Steps: verify the archive checksums -> take a safety backup of what is running now (unless --no-safety-backup) -> stop web
# and worker -> pg_restore --clean -> run migrations (an older dump is brought to the running release) -> start everything ->
# healthcheck. On a new server with no .env, the archived .env is used automatically; its database password then initialises
# the new empty database volume.
set -euo pipefail
# shellcheck source=deploy/live/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ARCHIVE=""; RESTORE_ENV=0; ASSUME_YES=0; SAFETY=1
while [ $# -gt 0 ]; do
  case "$1" in
    --restore-env) RESTORE_ENV=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --no-safety-backup) SAFETY=0; shift ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) [ -z "$ARCHIVE" ] || die "one archive only."; ARCHIVE="$1"; shift ;;
  esac
done
[ -n "$ARCHIVE" ] || die "usage: restore.sh ARCHIVE.tar [--restore-env] [--yes] [--no-safety-backup]"
[ -f "$ARCHIVE" ] || die "$ARCHIVE not found."
need_cmd docker "Install Docker."
need_cmd tar "Install tar."

umask 077
work="$(mktemp -d "${TMPDIR:-/tmp}/linkedeye-restore.XXXXXX")"
trap 'rm -rf "$work"' EXIT
tar -C "$work" -xf "$ARCHIVE" || die "$ARCHIVE is not a readable tar archive."
for f in db.dump env MANIFEST SHA256SUMS; do
  [ -f "$work/$f" ] && [ ! -L "$work/$f" ] || die "$ARCHIVE does not look like a LinkedEye backup (missing or not a plain file: $f)."
done
( cd "$work" && { sha256sum -c --quiet SHA256SUMS 2>/dev/null || shasum -a 256 -c --quiet SHA256SUMS; } ) || die "checksum mismatch: the archive is damaged. Nothing was changed."
ok "archive verified"
sed 's/^/     /' "$work/MANIFEST"

archived_env_value() { grep -m1 "^$1=" "$work/env" | cut -d= -f2- || true; }

if [ ! -f "$ENV_FILE" ]; then
  cp "$work/env" "$ENV_FILE"; chmod 600 "$ENV_FILE"
  ok "no .env on this host: using the archived one"
elif [ "$RESTORE_ENV" -eq 1 ]; then
  cp "$ENV_FILE" "$ENV_FILE.before-restore-$(timestamp)"
  chmod 600 "$ENV_FILE.before-restore-"*
  # The database role and name live in the PostgreSQL volume, not in the dump: the running database still expects the
  # CURRENT password, so keep the current values of these three (everything else comes from the archive).
  keep_db_user="$(get_env LINKEDEYE_DB_USER)"; keep_db_name="$(get_env LINKEDEYE_DB_NAME)"; keep_db_password="$(get_env LINKEDEYE_DB_PASSWORD)"
  cp "$work/env" "$ENV_FILE"; chmod 600 "$ENV_FILE"
  { [ -z "$keep_db_user" ] || set_env LINKEDEYE_DB_USER "$keep_db_user"; }
  { [ -z "$keep_db_name" ] || set_env LINKEDEYE_DB_NAME "$keep_db_name"; }
  { [ -z "$keep_db_password" ] || set_env LINKEDEYE_DB_PASSWORD "$keep_db_password"; }
  ok "archived .env restored (the previous one was kept as .env.before-restore-*; the database login of this server was kept)"
else
  for key in LINKEDEYE_CREDENTIAL_KEY LINKEDEYE_SECRET_KEY LINKEDEYE_DB_PASSWORD; do
    if [ "$(archived_env_value "$key")" != "$(get_env "$key")" ]; then
      warn "$key differs from the archived .env."
      [ "$key" != LINKEDEYE_CREDENTIAL_KEY ] || warn "Stored credentials in this backup stay unreadable until you use the archived key (--restore-env)."
    fi
  done
fi

if [ "$ASSUME_YES" -eq 0 ]; then
  [ -t 0 ] || die "refusing to restore without --yes when there is no terminal."
  say ""
  say "This REPLACES the database of the running stack with the contents of $ARCHIVE."
  read -r -p "Type RESTORE to continue: " answer
  [ "$answer" = RESTORE ] || die "cancelled. Nothing was changed."
fi

if [ "$SAFETY" -eq 1 ] && [ "$(service_state db)" = healthy ]; then
  say "Safety backup of the current state ..."
  "$LIVE_DIR/backup.sh" --quiet >/dev/null || die "the safety backup failed; use --no-safety-backup to skip it."
fi

say "Stopping web and worker ..."
quiet dc stop web worker caddy || true
quiet dc up -d db || die "could not start the database."
wait_healthy db 120 || die "the database did not become healthy."

say "Restoring the database ..."
# --single-transaction: all or nothing. Without it a failure half-way (a view that depends on a table, a dump from a newer
# PostgreSQL) leaves the database with the old tables already dropped and the new ones missing.
dc exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-acl --single-transaction --exit-on-error' < "$work/db.dump" \
  || die "pg_restore failed and was rolled back: the database is exactly as it was. web, worker and caddy are stopped; start them with: docker compose -f deploy/live/docker-compose.live.yml up -d"
ok "database restored"

say "Migrating to the running release and starting the stack ..."
quiet dc up -d || die "docker compose up failed after the restore."
wait_healthy web 180 || die "web did not become healthy after the restore."
wait_healthy caddy 90 || true
"$LIVE_DIR/healthcheck.sh" || warn "healthcheck reported problems."
ok "restore finished. Check the sign-in page and Administration > System health."
