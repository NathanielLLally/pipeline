#!/usr/bin/env bash
# Installs the worker watchdog as a systemd *user* timer.
#
# User units rather than system units, because the watchdog runs entirely as the
# REST_SSH_USER account: it reads that account's .env, uses its SSH keys to reach the
# worker hosts, and needs no root privilege at all. Installing it system-wide would mean
# either running it as root or teaching it to drop privileges, both for no gain.
#
# Run this ON the host that should carry the watchdog (normally the database host,
# accurateleadinfo.com -- note SSH there is on port 2222, not 22):
#
#   ./deploy/install-watchdog.sh              # install and start
#   ./deploy/install-watchdog.sh --dry-run    # show what would happen
#   ./deploy/install-watchdog.sh --uninstall  # stop, disable, remove
#
# Idempotent: re-running it re-renders the units and restarts the timer, which is the
# normal way to deploy a change to the watchdog script itself.
set -euo pipefail

TOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SRC_DIR="$TOP/deploy/systemd"
UNITS=(leads-watchdog.service leads-watchdog.timer)

DRY_RUN=0
UNINSTALL=0
while (($# > 0)); do
  case "$1" in
    --dry-run)   DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)   sed -n '2,20p' "$0"; exit 0 ;;
    *)           echo "Unknown flag: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
run()  { if ((DRY_RUN)); then say "  would run: $*"; else "$@"; fi; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

if ((UNINSTALL)); then
  say "Uninstalling watchdog user units..."
  run systemctl --user disable --now leads-watchdog.timer || true
  for u in "${UNITS[@]}"; do run rm -f "$UNIT_DIR/$u"; done
  run systemctl --user daemon-reload
  say "Removed. Historical ticks in leads.worker_health_log are left untouched."
  exit 0
fi

#===== PREFLIGHT =====
# Each check below corresponds to a way this deployment has actually gone wrong before.

[[ -x "$TOP/scripts/worker-watchdog.sh" ]] \
  || fail "scripts/worker-watchdog.sh missing or not executable in $TOP"

[[ -f "$TOP/.env" ]] \
  || fail ".env not found in $TOP -- the watchdog sources it for DB and SSH credentials"

command -v psql >/dev/null || fail "psql not found in PATH"

# systemd user units only survive logout if lingering is enabled. Without it the timer
# dies when the installing session ends, which looks exactly like "the watchdog silently
# stopped working" days later.
if ! loginctl show-user "$(id -un)" --property=Linger 2>/dev/null | grep -q 'Linger=yes'; then
  say "WARNING: lingering is not enabled for $(id -un)."
  say "         The timer will stop when your session ends. Enable it with:"
  say "           sudo loginctl enable-linger $(id -un)"
fi

# The fleet-aware watchdog reads SCRAPER_SSH_HOSTS (a bash array). With only the legacy
# singular SCRAPER_SSH_HOST it falls back cleanly rather than failing -- which is worse
# than failing, because the result is a watchdog that supervises one worker out of three
# and reports HEALTHY while the other two are dead.
if grep -qE '^[[:space:]]*SCRAPER_SSH_HOSTS=' "$TOP/.env"; then
  say "ok: SCRAPER_SSH_HOSTS is set -- fleet-aware probing enabled"
elif grep -qE '^[[:space:]]*SCRAPER_SSH_HOST=' "$TOP/.env"; then
  say "WARNING: .env has only the singular SCRAPER_SSH_HOST."
  say "         The watchdog will supervise ONE worker and cannot detect the others"
  say "         dying. Add the array form before relying on it, e.g.:"
  say "           SCRAPER_SSH_HOSTS=(worker.example.com worker2.example.com worker3.example.com)"
else
  fail "neither SCRAPER_SSH_HOSTS nor SCRAPER_SSH_HOST found in .env"
fi

# A watchdog already running elsewhere writes to the same worker_health_log table. That
# is survivable (ticks interleave) but should be a deliberate choice, not a surprise.
if systemctl --user is-active --quiet leads-watchdog.timer 2>/dev/null; then
  say "note: leads-watchdog.timer is already active here; it will be restarted."
fi

#===== VERIFY THE SCRIPT RUNS =====
# Catches a broken .env or unreachable DB now, rather than as a silent stream of failed
# ticks in the journal.
say "Checking the watchdog runs (dry-run tick)..."
if ((DRY_RUN)); then
  say "  would run: $TOP/scripts/worker-watchdog.sh --dry-run"
elif ! timeout 90 "$TOP/scripts/worker-watchdog.sh" --dry-run >/dev/null 2>&1; then
  fail "worker-watchdog.sh --dry-run failed; fix that before installing the timer"
else
  say "ok: dry-run tick succeeded"
fi

#===== INSTALL =====
say "Installing units into $UNIT_DIR (LEADS_ROOT=$TOP)"
run mkdir -p "$UNIT_DIR"
for u in "${UNITS[@]}"; do
  [[ -f "$SRC_DIR/$u" ]] || fail "missing template $SRC_DIR/$u"
  if ((DRY_RUN)); then
    say "  would render: $SRC_DIR/$u -> $UNIT_DIR/$u"
  else
    sed "s|@LEADS_ROOT@|$TOP|g" "$SRC_DIR/$u" > "$UNIT_DIR/$u"
  fi
done

run systemctl --user daemon-reload
run systemctl --user enable --now leads-watchdog.timer

if ((DRY_RUN)); then
  say "Dry run complete; nothing was changed."
  exit 0
fi

say ""
say "Installed. Verify with:"
say "  systemctl --user list-timers leads-watchdog.timer"
say "  journalctl --user -u leads-watchdog.service -n 50 --no-pager"
say "  psql \"\$LEADS_DB_URL\" -c \"SELECT checked_at, verdict FROM leads.worker_health_log ORDER BY checked_at DESC LIMIT 5;\""
say ""
say "The first tick lands within ~60s. To deploy a later change to the watchdog:"
say "  git pull && ./deploy/install-watchdog.sh"
