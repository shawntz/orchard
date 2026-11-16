#!/usr/bin/env bash
# Update Orchard on the server to the latest commit on GitHub and restart it.
#
#   /opt/orchard/scripts/update.sh              # update to origin/main (re-runs itself with sudo)
#   /opt/orchard/scripts/update.sh -y           # don't ask if jobs are running
#   /opt/orchard/scripts/update.sh --no-backup  # skip the S3 snapshot
#   BRANCH=dev /opt/orchard/scripts/update.sh
#
# Steps: fetch, take a backup snapshot, stop Orchard, fast-forward to the latest
# commit, start Orchard, wait until it answers. If anything fails after Orchard
# is stopped, it is started again on whatever code is checked out.
#
# Matches the layout in docs/deploy-aws.md: code in /opt/orchard, data in
# /var/lib/orchard, systemd unit "orchard", optional "orchard-snapshot" unit.

# Everything runs inside main(), so bash has read the whole script before
# "git merge" replaces this file on disk.
main() {
  set -euo pipefail

  local repo="${REPO:-/opt/orchard}"
  local service="${SERVICE:-orchard}"
  local branch="${BRANCH:-main}"
  local db="${DB:-/var/lib/orchard/orchard.sqlite3}"
  local port="${PORT:-8765}"
  local assume_yes=0 backup=1

  for arg in "$@"; do
    case "$arg" in
      -y|--yes) assume_yes=1 ;;
      --no-backup) backup=0 ;;
      -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) echo "update: unknown option '$arg' (try --help)" >&2; exit 2 ;;
    esac
  done

  if [ "$(id -u)" -ne 0 ]; then
    exec sudo REPO="$repo" SERVICE="$service" BRANCH="$branch" DB="$db" PORT="$port" "$0" "$@"
  fi

  if [ -t 1 ]; then say() { printf '\033[1m==>\033[0m %s\n' "$*"; }; else say() { printf '==> %s\n' "$*"; }; fi
  die() { printf 'update: %s\n' "$*" >&2; exit 1; }

  cd "$repo" || die "no repository at $repo"
  git rev-parse --git-dir >/dev/null 2>&1 || die "$repo is not a git repository"

  # 1. Fetch and make sure the update can be applied before stopping anything.
  say "Fetching origin/$branch"
  git fetch --quiet origin "$branch"
  local old new
  old=$(git rev-parse HEAD)
  new=$(git rev-parse "origin/$branch")

  if [ "$old" = "$new" ]; then
    say "Already up to date at $(git log -1 --format='%h %s' HEAD)"
    systemctl is-active --quiet "$service" || { say "Orchard is not running; starting it"; systemctl start "$service"; }
    exit 0
  fi

  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git status --short --untracked-files=no >&2
    die "files in $repo were edited on the server. Commit, stash or 'git checkout -- .' them first."
  fi
  git merge-base --is-ancestor HEAD "$new" ||
    die "$(git rev-parse --short HEAD) is not an ancestor of origin/$branch, so it can't fast-forward. Check out origin/$branch by hand."

  echo
  git --no-pager log --format='  %h %s (%an)' "$old..$new"
  echo

  # 2. Warn about jobs that the restart would interrupt. Each table is counted on
  #    its own so an older database that lacks some of them still gets checked.
  local active=0 owner table states n
  if command -v sqlite3 >/dev/null && [ -f "$db" ]; then
    owner=$(stat -c %U "$db")
    while read -r table states; do
      n=$(sudo -u "$owner" sqlite3 "$db" "SELECT COUNT(*) FROM $table WHERE status IN ($states)" 2>/dev/null) || n=0
      [[ "$n" =~ ^[0-9]+$ ]] && active=$((active + n))
    done <<'JOBS'
runs             'queued','running'
eyelink_scans    'queued','running'
discovery_jobs   'queued','running','inspecting','searching','reading_documentation'
download_batches 'queued','running'
JOBS
  fi
  if [ "$active" -gt 0 ] && [ "$assume_yes" -eq 0 ]; then
    echo "$active background job(s) are queued or running. Restarting marks them 'interrupted'."
    [ -t 0 ] || die "not restarting while jobs run; nothing changed. Re-run with -y (or the Deploy workflow with force) to restart anyway."
    read -r -p "Restart anyway? [y/N] " reply || reply=
    case "$reply" in [yY]*) ;; *) die "cancelled, nothing changed" ;; esac
  fi

  # 3. Backup (the same snapshot the daily timer takes).
  if [ "$backup" -eq 1 ] && systemctl cat orchard-snapshot.service >/dev/null 2>&1; then
    say "Taking a backup snapshot"
    systemctl start orchard-snapshot.service ||
      die "backup failed (see: journalctl -u orchard-snapshot). Nothing changed. Use --no-backup to skip it."
  fi

  # 4. Stop, update, start. From here on, always try to bring Orchard back up.
  trap 'echo "update: failed; starting Orchard again on $(git rev-parse --short HEAD)" >&2; systemctl start "$service" || true' ERR
  local started
  started=$(date +%s)
  say "Stopping $service"
  systemctl stop "$service"
  say "Updating to $(git rev-parse --short "$new")"
  git checkout --quiet "$branch" 2>/dev/null || git checkout --quiet -B "$branch" "origin/$branch"
  git merge --quiet --ff-only "$new"
  say "Starting $service"
  systemctl start "$service"
  trap - ERR

  # 5. Wait until it answers on its local port (any HTTP status means it is up).
  local code=000
  for _ in $(seq 1 20); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/" 2>/dev/null) || code=000
    [ "$code" != 000 ] && break
    sleep 1
  done

  # Startup messages only. Request lines name the signed-in user, and this output
  # also ends up in the (public) GitHub Actions log when the Deploy workflow runs.
  journalctl -u "$service" --since "@$started" --no-pager --output=cat 2>/dev/null |
    grep -vE '"(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) ' | tail -n 12 || true
  echo
  if [ "$code" = 000 ]; then
    die "Orchard did not answer on port $port after 20s. See: journalctl -u $service -n 50"
  fi
  say "Updated $(git rev-parse --short "$old") -> $(git log -1 --format='%h %s' HEAD)"
  exit 0
}

main "$@"
