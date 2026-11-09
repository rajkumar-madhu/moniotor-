#!/usr/bin/env bash
# Update the live stack to the current checkout (or a given git ref) with a backup first.
#
#   ./deploy/live/update.sh                    # git pull --ff-only, backup, build, migrate, restart, healthcheck
#   ./deploy/live/update.sh --ref v9.1         # check out a tag or commit instead of pulling
#   ./deploy/live/update.sh --no-pull          # you already updated the working tree yourself
#   ./deploy/live/update.sh --pull-image       # pull LINKEDEYE_IMAGE from a registry instead of building
#   ./deploy/live/update.sh --no-build         # run the image that already exists locally (built or loaded elsewhere)
#   ./deploy/live/update.sh --yes              # do not ask
#
# Migrations only move forward. If the new release misbehaves, "roll back" means: old code + the backup taken here:
#   git checkout <previous revision printed below> && docker compose -f deploy/live/docker-compose.live.yml build
#   ./deploy/live/restore.sh <backup printed below> --yes
# (without a schema change in the release you can skip the restore: build the old revision and `docker compose up -d`).
set -euo pipefail
# shellcheck source=deploy/live/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

REF=""; NO_PULL=0; PULL_IMAGE=0; ASSUME_YES=0; NO_BUILD=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?--ref needs a tag, branch or commit}"; shift 2 ;;
    --no-pull) NO_PULL=1; shift ;;
    --pull-image) PULL_IMAGE=1; NO_PULL=1; shift ;;
    --no-build) NO_BUILD=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done
need_cmd docker "Install Docker."
[ -f "$ENV_FILE" ] || die "$ENV_FILE not found: run bootstrap.sh first."

repo="$(git -C "$LIVE_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
before="unknown"
[ -z "$repo" ] || before="$(git -C "$repo" rev-parse --short HEAD)"
say "Current revision: $before"

if [ "$ASSUME_YES" -eq 0 ]; then
  [ -t 0 ] || die "no terminal: pass --yes to update unattended."
  read -r -p "Update now? A backup is taken first. [y/N] " answer
  case "$answer" in y|Y|yes) ;; *) die "cancelled." ;; esac
fi

if [ "$NO_PULL" -eq 0 ] || [ -n "$REF" ]; then
  [ -n "$repo" ] || die "this is not a git checkout; copy the new release over it and use --no-pull."
  if [ -n "$(git -C "$repo" status --porcelain --untracked-files=no)" ]; then
    die "the working tree has local changes; commit or stash them first (nothing was changed)."
  fi
  case "$REF" in -*) die "--ref '$REF' looks like an option, not a tag, branch or commit." ;; esac
  if [ -n "$REF" ]; then git -C "$repo" fetch --tags --quiet && git -C "$repo" checkout --quiet "$REF" --
  else git -C "$repo" pull --ff-only --quiet; fi
fi
after="unknown"; [ -z "$repo" ] || after="$(git -C "$repo" rev-parse --short HEAD)"
say "New revision:     $after"

say "Backing up first ..."
backup="$("$LIVE_DIR/backup.sh" --quiet)" || die "the backup failed; the stack was not touched."
ok "backup: $backup"

# remember what runs now, for the rollback hint
cid="$(container_of web)"
if [ -n "$cid" ]; then
  prev_image="$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || true)"
  { echo "revision=$before"; echo "backup=$backup"; echo "image_id=$prev_image"; echo "updated_to=$after"; } > "$LIVE_DIR/.last-update"
  [ -z "$prev_image" ] || docker tag "$prev_image" "linkedeye-live-rollback:$before" 2>/dev/null || true
fi

say "Building / pulling ..."
if [ "$NO_BUILD" -eq 1 ]; then note "(--no-build: using the local image)"
elif [ "$PULL_IMAGE" -eq 1 ]; then dc pull web || die "could not pull the image; the stack was not touched."
else dc build web || die "the build failed; the stack was not touched."; fi
dc pull --ignore-buildable --quiet 2>/dev/null || warn "could not pull the third-party images; keeping the ones on this host."

say "Migrating and restarting (migrate runs first; web and worker are recreated after it) ..."
quiet dc up -d || rc=1
[ "${rc:-0}" -ne 0 ] || wait_healthy web 180 || rc=1
[ "${rc:-0}" -ne 0 ] || wait_healthy caddy 90 || true
if [ "${rc:-0}" -eq 0 ] && "$LIVE_DIR/healthcheck.sh" --quiet; then
  ok "updated $before -> $after"
  note "the previous image is tagged linkedeye-live-rollback:$before; the pre-update backup is $backup"
  exit 0
fi
warn "The new release is not healthy. Nothing was rolled back automatically (migrations may already have run)."
say "  1. look: docker compose -f deploy/live/docker-compose.live.yml logs --tail=100 migrate web worker"
say "  2. roll back: git checkout $before && docker compose -f deploy/live/docker-compose.live.yml build"
say "                ./deploy/live/restore.sh $backup --yes"
exit 1
