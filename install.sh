#!/bin/sh
#
# tokentab installer — idempotent, POSIX sh, no dependency but python3.
#
# Run it from a checkout on the target host:
#
#   ./install.sh --collect --server <server-host>          # a collecting host
#   ./install.sh --serve --bind <addr> --collect           # the server
#
# It lays out four directories with four different lifetimes, so an upgrade is
# "re-run this script" and never risks the two things that cannot be rebuilt:
#
#   ~/.local/share/tokentab/   code + web/dist   replaced wholesale every run
#   ~/.config/tokentab/        rates, history, plans   plans.json is NEVER overwritten
#   ~/.local/share/tokentab/tokentab.db          the store — never touched here
#   ~/.local/state/tokentab/   scan state, logs  prunable
#
# It deliberately does NOT migrate an existing `aispend` install. Moving a
# 100 MB+ store is a one-time, verify-then-delete operation that wants a human
# watching it, not a flag on a script that gets re-run on every upgrade; the
# script only detects the old install and prints the commands.
set -eu

# shellcheck disable=SC1007  # `CDPATH= cd` is the idiom, not a broken assignment
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LIB="$HOME/.local/share/tokentab"
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/tokentab"
STATE="$HOME/.local/state/tokentab"
BIN="$HOME/.local/bin"

DO_SERVE=0
DO_COLLECT=0
BIND="127.0.0.1"
PORT="8899"
SERVER=""

usage() {
  cat <<EOF
usage: install.sh [--serve] [--collect] [--bind ADDR] [--port N] [--server HOST]

  --serve          install + enable the dashboard service (the server host only)
  --collect        install + enable the hourly collector
  --bind ADDR      dashboard bind address (default 127.0.0.1 — never 0.0.0.0)
  --port N         dashboard port (default 8899)
  --server HOST    ssh host running \`tokentab ingest\`; makes the collector
                   push there instead of writing to a local store

With neither --serve nor --collect, only the code, config and wrapper are
installed and no service is touched.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --serve)   DO_SERVE=1 ;;
    --collect) DO_COLLECT=1 ;;
    --bind)    BIND="$2"; shift ;;
    --port)    PORT="$2"; shift ;;
    --server)  SERVER="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install.sh: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

PYTHON=$(command -v python3 || true)
[ -n "$PYTHON" ] || { echo "install.sh: python3 not found on PATH" >&2; exit 1; }

say() { printf '  %s\n' "$*"; }

# ---------------------------------------------------------------- code

mkdir -p "$LIB" "$CFG" "$STATE" "$BIN"

cp "$SRC/tokentab.py" "$LIB/tokentab.py"
chmod 644 "$LIB/tokentab.py"
say "code      $LIB/tokentab.py"

# The SPA is a build artefact and is gitignored, so a fresh clone has no dist/.
# Absent, leave whatever is already installed alone rather than deleting a
# working dashboard: `cd web && npm ci && npm run build` then re-run.
if [ -d "$SRC/web/dist" ]; then
  rm -rf "$LIB/web/dist"
  mkdir -p "$LIB/web"
  cp -R "$SRC/web/dist" "$LIB/web/dist"
  say "ui        $LIB/web/dist"
elif [ -d "$LIB/web/dist" ]; then
  say "ui        kept existing (no web/dist in source — run the web build first)"
else
  say "ui        NOT installed — no web/dist here; the server will serve a stub"
fi

# ---------------------------------------------------------------- config

# rates.json is repo-owned (published list prices), so the repo wins on upgrade.
# price_history.json goes with it, always: it says what those prices used to be,
# so a copy older than its rates.json would miss records and silently re-price
# past days at today's rate.
#
# The log goes first, for the same reason tools/update-rates.py writes it first:
# an interrupt between the two must not leave new prices beside a log that has
# not learned the old ones. History-then-rates leaves the store priced exactly
# as it was; the other order re-prices every past day at today's rate.
#
# Absent upstream is not "keep what is there" — a repo with no log is a repo
# where no price has moved, and a stale copy would price past days at rates
# nothing here still publishes. It is removed.
if [ -f "$SRC/price_history.json" ]; then
  cp "$SRC/price_history.json" "$CFG/price_history.json"
  say "history   $CFG/price_history.json"
else
  rm -f "$CFG/price_history.json"
fi
cp "$SRC/rates.json" "$CFG/rates.json"
say "rates     $CFG/rates.json"

# plans.json names the real accounts. It is gitignored, it is the one file here
# a human edits, and it is not recoverable from the repo — so it is written once
# and never again.
if [ -f "$CFG/plans.json" ]; then
  say "plans     $CFG/plans.json (kept — installer never overwrites it)"
elif [ -f "$SRC/plans.json" ]; then
  cp "$SRC/plans.json" "$CFG/plans.json"
  say "plans     $CFG/plans.json (from source)"
else
  cp "$SRC/plans.example.json" "$CFG/plans.json"
  say "plans     $CFG/plans.json (from the example — EDIT IT: fees and accounts are placeholders)"
fi

# ---------------------------------------------------------------- wrapper

cat > "$BIN/tokentab" <<EOF
#!/bin/sh
# tokentab — unified AI spend tracking. Installed by install.sh; edits are lost
# on the next run. Source: https://github.com/4242labs/tokentab
exec $PYTHON "$LIB/tokentab.py" "\$@"
EOF
chmod 755 "$BIN/tokentab"
say "wrapper   $BIN/tokentab"

# ---------------------------------------------------------------- version

# What is actually deployed, so an audit can diff the host against the repo
# instead of trusting a doc.
VERSION=$(git -C "$SRC" describe --tags --always --dirty 2>/dev/null || echo unknown)
printf '%s\n%s\n' "$VERSION" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$LIB/VERSION"
say "version   $VERSION"

# ---------------------------------------------------------------- services

svc_systemd() {
  UD="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$UD"

  if [ "$DO_SERVE" = 1 ]; then
    cat > "$UD/tokentab-serve.service" <<EOF
[Unit]
Description=tokentab dashboard (tailnet-only)
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/tokentab serve --bind $BIND --port $PORT
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
EOF
  fi

  if [ "$DO_COLLECT" = 1 ]; then
    if [ -n "$SERVER" ]; then
      EXEC="%h/.local/bin/tokentab push --server $SERVER"
    else
      EXEC="/bin/sh -c \"%h/.local/bin/tokentab scan | %h/.local/bin/tokentab ingest\""
    fi
    cat > "$UD/tokentab-collect.service" <<EOF
[Unit]
Description=tokentab collector (local transcripts -> store)

[Service]
Type=oneshot
ExecStart=$EXEC
EOF
    cat > "$UD/tokentab-collect.timer" <<EOF
[Unit]
Description=tokentab collector hourly

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
EOF
  fi

  systemctl --user daemon-reload
  if [ "$DO_SERVE" = 1 ]; then
    systemctl --user enable --now tokentab-serve.service
    say "service   tokentab-serve.service -> http://$(hostname -s):$PORT/"
  fi
  if [ "$DO_COLLECT" = 1 ]; then
    systemctl --user enable --now tokentab-collect.timer
    say "timer     tokentab-collect.timer (hourly)"
  fi
}

svc_launchd() {
  LA="$HOME/Library/LaunchAgents"
  mkdir -p "$LA"

  # launchd has no oneshot-timer split: StartInterval on the agent itself is the
  # timer. A serve agent would want KeepAlive instead — not written here because
  # the only Mac in the fleet collects, it does not serve.
  if [ "$DO_SERVE" = 1 ]; then
    echo "install.sh: --serve is not implemented for launchd (no Mac serves)" >&2
    exit 2
  fi

  if [ "$DO_COLLECT" = 1 ]; then
    [ -n "$SERVER" ] || { echo "install.sh: --collect on macOS needs --server HOST" >&2; exit 2; }
    P="$LA/com.42labs.tokentab-collect.plist"
    cat > "$P" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.42labs.tokentab-collect</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN/tokentab</string>
    <string>push</string>
    <string>--server</string>
    <string>$SERVER</string>
  </array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$STATE/collect.log</string>
  <key>StandardErrorPath</key><string>$STATE/collect.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/com.42labs.tokentab-collect" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$P"
    say "agent     com.42labs.tokentab-collect (hourly)"
  fi
}

if [ "$DO_SERVE" = 1 ] || [ "$DO_COLLECT" = 1 ]; then
  case "$(uname -s)" in
    Darwin) svc_launchd ;;
    *)      svc_systemd ;;
  esac
fi

# ---------------------------------------------------------------- old install

# Detected, never acted on: see the header. The store is the only thing in this
# system that cannot be re-collected — vendors prune the transcripts.
if [ -d "$HOME/.local/share/aispend" ]; then
  cat <<EOF

  NOTE: an old 'aispend' install is still on this host. It is untouched.
  Retire it by hand, in this order, once the tokentab units are confirmed good:

    systemctl --user disable --now aispend-serve.service aispend-collect.timer
    mv ~/.local/share/aispend/aispend.db $LIB/tokentab.db   # move, never copy
    tokentab report                                          # verify the count
    rm -rf ~/.local/share/aispend ~/.local/bin/aispend
    rm ~/.config/systemd/user/aispend-*

EOF
fi

printf '\ntokentab %s installed.\n' "$VERSION"

# An upgrade is "re-run this script", and a store written before events carried
# their own Value has none on its old rows. Every report, the dashboard and the
# status line refuse to add up a period holding one — correctly, rather than
# answering $0.00 — so the upgrade has one manual step, and this is the only
# place that would otherwise not mention it.
DB="${TOKENTAB_DB:-$LIB/tokentab.db}"
if [ -f "$DB" ] && python3 -c '
import pathlib, sqlite3, sys
# as_uri, not concatenation: a "#" in the path truncates the URI and drops
# mode=ro, and a read-only probe that writes is not one.
uri = pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro"
try:
    con = sqlite3.connect(uri, uri=True, timeout=2)
    unpriced = con.execute("SELECT 1 FROM events WHERE value_usd IS NULL LIMIT 1").fetchone()
except sqlite3.DatabaseError as e:
    # No such column means a store older than the migration: every row needs
    # pricing. Anything else — locked, corrupt, not a store — is not something
    # to advise a reprice about, so say nothing.
    unpriced = "no such column" in str(e)
sys.exit(0 if unpriced else 1)
' "$DB"; then
  cat <<EOF

  ACTION NEEDED: events already in the store carry no Value yet, and no report
  will add up until they do. One command, once:

    tokentab reprice --apply

  It prices each event at the rate of its own day. A minute or so per million.

EOF
fi
