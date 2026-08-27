#!/usr/bin/env python3
"""tokentab — unified AI spend tracking across providers, projects and machines.

One dashboard answering three questions for any slice of providers × projects ×
timeframe × machine × model:

  Cash out   real money that left the account in the period
  Allocated  a flat plan's fee split across projects by token share (NOT measured —
             a subscription has no per-call price, so this is an accounting split)
  Value      the same usage priced at published API list rates, covering
             subscription and local-model traffic alike

Sources (all local, no vendor API needed):
  claude_code   ~/.claude/projects/<encoded-cwd>/<session>.jsonl   (usage blocks)
  codex         ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl       (token_count events)
  llama_swap    llama-swap log (llama.cpp timing lines) — local models, $0 cash

Subcommands
  scan       parse local transcripts, emit NDJSON events on stdout (incremental)
  push       scan | ssh <server> tokentab ingest
  ingest     read NDJSON on stdin into the SQLite store
  serve      run the dashboard (bind tailnet address only)
  report     CLI summary — same numbers as the dashboard (--json to script against)
  statusline one line of current spend, for a shell prompt or an agent status bar
  verify     acceptance checks: allocation conservation + rate spot-check
  backfill   scan everything on disk, ignoring saved offsets

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import glob
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("TOKENTAB_STATE", "~/.local/state/tokentab")).expanduser()
DB_PATH = Path(os.environ.get("TOKENTAB_DB", "~/.local/share/tokentab/tokentab.db")).expanduser()


def _config_dir() -> Path:
    """Where rates.json and plans.json live.

    Installed, config sits in XDG config and the code in ~/.local/share, so an
    upgrade can replace the code wholesale without taking the accounts with it.
    Run from a checkout there is no XDG copy, and the repo dir is the config
    dir — which is what makes `./tokentab.py report` work with no install.
    """
    env = os.environ.get("TOKENTAB_CONFIG")
    if env:
        return Path(env).expanduser()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "tokentab"
    return xdg if (xdg / "rates.json").exists() else HERE


CONFIG_DIR = _config_dir()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id             TEXT PRIMARY KEY,
  ts             TEXT NOT NULL,
  day            TEXT NOT NULL,
  host           TEXT NOT NULL,
  source         TEXT NOT NULL,
  provider       TEXT NOT NULL,
  billing        TEXT NOT NULL,          -- flat | metered | local
  plan           TEXT,
  account        TEXT NOT NULL DEFAULT '',  -- the vendor login the usage was billed to
  model          TEXT NOT NULL,
  project        TEXT NOT NULL,
  repo           TEXT NOT NULL,
  session        TEXT,
  input          INTEGER NOT NULL DEFAULT 0,
  output         INTEGER NOT NULL DEFAULT 0,
  cache_read     INTEGER NOT NULL DEFAULT 0,
  cache_write    INTEGER NOT NULL DEFAULT 0,
  cache_write_1h INTEGER NOT NULL DEFAULT 0,
  cash_usd       REAL NOT NULL DEFAULT 0  -- metered only; flat/local are always 0
);
CREATE INDEX IF NOT EXISTS ix_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_day     ON events(day);
CREATE INDEX IF NOT EXISTS ix_events_project ON events(project);
-- Covers the subscription discovery in plan_instances: with `day` in the index
-- its MIN/MAX need no row lookups at all. 551ms -> 60ms over 400k events, which
-- every report, dashboard and status line was paying. It replaces (plan,
-- account, ts) — nothing queries `ts` — so the store gets 12MB smaller too.
DROP INDEX IF EXISTS ix_events_plan;
CREATE INDEX IF NOT EXISTS ix_events_plan_day ON events(plan, account, day);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

# Columns added after the first release. Applied to any store already on disk —
# sqlite has no "add column if not exists".
MIGRATIONS = (("account", "ALTER TABLE events ADD COLUMN account TEXT NOT NULL DEFAULT ''"),)

TOKEN_COLS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")


# ---------------------------------------------------------------- config


def load_json(name: str) -> dict:
    return json.loads((CONFIG_DIR / name).read_text())


def load_rates() -> dict:
    return load_json("rates.json")


def load_plans() -> dict:
    return load_json("plans.json")


def account_label(plans: dict, account: str) -> str:
    """Optional display name for an account; the raw login otherwise."""
    return plans.get("accounts", {}).get(account, {}).get("label") or account


def host_name(plans: dict) -> str:
    h = (os.environ.get("TOKENTAB_HOST") or socket.gethostname()).split(".")[0].lower()
    return plans.get("hosts", {}).get(h, h.upper())


# ---------------------------------------------------------------- accounts


def _jwt_email(token: str) -> str:
    """Email claim out of an id_token. The token itself is never read further,
    never stored and never logged — only this one claim leaves the function."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return ""
    return str(data.get("email") or "")


def claude_account() -> str:
    """Which Anthropic login this host's Claude Code is signed into.

    Transcripts carry no account identity, so the account is stamped at scan
    time from the CLI's own config. A host that switches accounts stamps only
    what it scans afterwards — history keeps the account it was scanned under.
    """
    p = Path(os.environ.get("CLAUDE_HOME", "~/.claude")).expanduser().parent / ".claude.json"
    try:
        acct = json.loads(p.read_text()).get("oauthAccount") or {}
    except (OSError, json.JSONDecodeError):
        return ""
    return str(acct.get("emailAddress") or acct.get("accountUuid") or "")


def codex_account() -> str:
    """Which OpenAI login this host's Codex CLI is signed into."""
    p = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "auth.json"
    try:
        auth = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    return _jwt_email((auth.get("tokens") or {}).get("id_token") or "")


# ---------------------------------------------------------------- attribution


WORKTREE_RE = re.compile(r"^(?P<repo>[^-]+(?:-[^-]+)*?)--")


def attribute(cwd: str | None, roots: list[str]) -> tuple[str, str]:
    """Map a working directory to (project, repo) by path position.

    <root>/<project>/<repo>/...                    -> (project, repo)
    <root>/<project>                               -> (project, project)
    <root>/<project>/worktrees/<repo>--<branch>    -> (project, repo)
    anything outside every root                    -> ('ad-hoc', <basename>)
    """
    if not cwd:
        return ("ad-hoc", "unknown")
    p = os.path.normpath(cwd)
    for root in roots:
        r = os.path.normpath(os.path.expanduser(root))
        if p == r:
            return ("ad-hoc", os.path.basename(r))
        if not p.startswith(r + os.sep):
            continue
        parts = p[len(r) + 1 :].split(os.sep)
        project = parts[0]
        if len(parts) == 1:
            return (project, project)
        if parts[1] == "worktrees":
            if len(parts) > 2:
                m = WORKTREE_RE.match(parts[2])
                return (project, m.group("repo") if m else parts[2])
            return (project, project)
        return (project, parts[1])
    return ("ad-hoc", os.path.basename(p) or "unknown")


VENDOR_PREFIXES = (
    ("claude-", ("anthropic", "flat", "claude-max")),
    ("gpt-", ("openai", "flat", "chatgpt")),
    ("o1", ("openai", "flat", "chatgpt")),
    ("o3", ("openai", "flat", "chatgpt")),
)


def classify_model(model: str, default: tuple[str, str, str]) -> tuple[str, str, str]:
    """(provider, billing, plan) for a model name.

    A subscription CLI can be pointed at a local endpoint — that traffic costs no
    money and must not consume the plan's allocation, so it is reclassified as
    local rather than inheriting the source's plan.
    """
    m = (model or "").lower()
    if m in ("<synthetic>", "", "unknown"):
        return default
    for prefix, cls in VENDOR_PREFIXES:
        if m.startswith(prefix):
            return cls
    return ("local", "local", None)


def event_id(*parts) -> str:
    return hashlib.sha1("\x1f".join(str(x) for x in parts).encode()).hexdigest()[:20]


def iso_utc(ts: str) -> str:
    """Normalise a timestamp to ISO-8601 UTC with a Z suffix."""
    t = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- scan state


class ScanState:
    """Byte offsets per transcript file, so a re-scan only reads what was appended."""

    def __init__(self, path: Path, fresh: bool = False):
        self.path = path
        self.data = {}
        if not fresh and path.exists():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError:
                self.data = {}

    def offset(self, f: str, size: int) -> int:
        rec = self.data.get(f)
        if not rec or rec.get("size", 0) > size:  # truncated/rotated -> start over
            return 0
        return rec.get("offset", 0)

    def set(self, f: str, offset: int, size: int) -> None:
        self.data[f] = {"offset": offset, "size": size}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data))


# ---------------------------------------------------------------- scanners


def scan_claude_code(state: ScanState, host: str, roots: list[str]):
    base = Path(os.environ.get("CLAUDE_HOME", "~/.claude")).expanduser() / "projects"
    account = claude_account()
    for f in sorted(glob.glob(str(base / "*" / "*.jsonl"))):
        try:
            size = os.path.getsize(f)
        except OSError:
            continue
        start = state.offset(f, size)
        if start >= size:
            continue
        pos = start
        with open(f, "rb") as fh:
            fh.seek(start)
            for raw in fh:
                pos += len(raw)
                if b'"usage"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage")
                if not isinstance(u, dict):
                    continue
                cc = u.get("cache_creation") or {}
                w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
                wall = int(u.get("cache_creation_input_tokens") or 0)
                project, repo = attribute(d.get("cwd"), roots)
                model = msg.get("model") or "unknown"
                provider, billing, plan = classify_model(model, ("anthropic", "flat", "claude-max"))
                yield {
                    "id": event_id("cc", d.get("sessionId"), d.get("uuid"), msg.get("id")),
                    "ts": iso_utc(d.get("timestamp") or ""),
                    "host": host,
                    "source": "claude_code",
                    "provider": provider,
                    "billing": billing,
                    "plan": plan,
                    "account": account if billing == "flat" else "",
                    "model": model,
                    "project": project,
                    "repo": repo,
                    "session": d.get("sessionId"),
                    "input": int(u.get("input_tokens") or 0),
                    "output": int(u.get("output_tokens") or 0),
                    "cache_read": int(u.get("cache_read_input_tokens") or 0),
                    "cache_write": max(wall - w1h, 0),
                    "cache_write_1h": w1h,
                    "cash_usd": 0.0,
                }
        state.set(f, pos, size)


def scan_codex(state: ScanState, host: str, roots: list[str]):
    base = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"
    account = codex_account()
    for f in sorted(glob.glob(str(base / "*" / "*" / "*" / "*.jsonl"))):
        try:
            size = os.path.getsize(f)
        except OSError:
            continue
        start = state.offset(f, size)
        if start >= size:
            continue
        # session_meta/turn_context live at the head of the file, so context is
        # re-read from byte 0 even when only the tail is new.
        cwd, model, session = None, None, None
        with open(f, "rb") as fh:
            for raw in fh:
                if b'"session_meta"' not in raw and b'"turn_context"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                p = d.get("payload") or {}
                cwd = p.get("cwd") or cwd
                model = p.get("model") or model
                session = p.get("session_id") or session
        project, repo = attribute(cwd, roots)
        pos = start
        with open(f, "rb") as fh:
            fh.seek(start)
            for raw in fh:
                pos += len(raw)
                if b'"token_count"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                p = d.get("payload") or {}
                info = p.get("info") or {}
                last = info.get("last_token_usage") or {}
                if not last:
                    continue
                inp = int(last.get("input_tokens") or 0)
                out = int(last.get("output_tokens") or 0)
                cread = int(last.get("cached_input_tokens") or 0)
                cwrite = int(last.get("cache_write_input_tokens") or 0)
                total = int(last.get("total_tokens") or 0)
                if total and not (inp or out or cread or cwrite):
                    inp = total  # imported/legacy turns report only a total
                if not (inp or out or cread or cwrite):
                    continue
                mdl = model or "gpt-5.6-terra"
                provider, billing, plan = classify_model(mdl, ("openai", "flat", "chatgpt"))
                yield {
                    "id": event_id("cx", session or f, d.get("timestamp"), total, out),
                    "ts": iso_utc(d.get("timestamp") or ""),
                    "host": host,
                    "source": "codex",
                    "provider": provider,
                    "billing": billing,
                    "plan": plan,
                    "account": account if billing == "flat" else "",
                    "model": mdl,
                    "project": project,
                    "repo": repo,
                    "session": session,
                    "input": inp,
                    "output": out,
                    "cache_read": cread,
                    "cache_write": cwrite,
                    "cache_write_1h": 0,
                    "cash_usd": 0.0,
                }
        state.set(f, pos, size)


LLAMA_PROMPT_RE = re.compile(rb"prompt eval time\s*=.*?/\s*(\d+)\s*tokens")
LLAMA_EVAL_RE = re.compile(rb"[^_]eval time\s*=.*?/\s*(\d+)\s*(?:runs|tokens)")
LLAMA_MODEL_RE = re.compile(rb"(?:starting|loading|swapping to)\s+model\s+[\"']?([\w.\-]+)", re.I)


def scan_llama_swap(state: ScanState, host: str):
    """llama-swap/llama.cpp write timing lines with no timestamp, so tokens are
    stamped at collection time. Day-resolution, and never project-attributed —
    the proxy sees no working directory. Both limits are surfaced in the UI."""
    log = Path(os.environ.get("LLAMA_SWAP_LOG", "~/.config/llama-swap/swap.log")).expanduser()
    if not log.exists():
        return
    size = log.stat().st_size
    start = state.offset(str(log), size)
    if start >= size:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    model = "local"
    agg: dict[str, dict[str, int]] = {}
    pos = start
    with open(log, "rb") as fh:
        fh.seek(start)
        for raw in fh:
            pos += len(raw)
            m = LLAMA_MODEL_RE.search(raw)
            if m:
                model = m.group(1).decode(errors="replace")
            pm = LLAMA_PROMPT_RE.search(raw)
            if pm:
                agg.setdefault(model, {"input": 0, "output": 0})["input"] += int(pm.group(1))
                continue
            em = LLAMA_EVAL_RE.search(raw)
            if em:
                agg.setdefault(model, {"input": 0, "output": 0})["output"] += int(em.group(1))
    for mdl, tok in agg.items():
        if not (tok["input"] or tok["output"]):
            continue
        yield {
            "id": event_id("ls", host, now, mdl),
            "ts": now,
            "host": host,
            "source": "llama_swap",
            "provider": "local",
            "billing": "local",
            "plan": None,
            "account": "",  # self-hosted: no vendor login, no fee
            "model": mdl,
            "project": "ad-hoc",
            "repo": "llama-swap",
            "session": None,
            "input": tok["input"],
            "output": tok["output"],
            "cache_read": 0,
            "cache_write": 0,
            "cache_write_1h": 0,
            "cash_usd": 0.0,
        }
    state.set(str(log), pos, size)


def cmd_scan(args) -> int:
    plans = load_plans()
    host = host_name(plans)
    roots = plans.get("roots", {}).get("paths", ["~/42labs"])
    state = ScanState(STATE_DIR / "scan-state.json", fresh=args.all)
    out = sys.stdout
    n = 0
    for gen in (
        scan_claude_code(state, host, roots),
        scan_codex(state, host, roots),
        scan_llama_swap(state, host),
    ):
        for ev in gen:
            if not ev["ts"]:
                continue
            out.write(json.dumps(ev, separators=(",", ":")) + "\n")
            n += 1
    out.flush()
    if not args.dry_run:
        state.save()
    print(f"tokentab scan: {n} events from {host}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- store


def connect(path: Path = DB_PATH, *, write: bool = True, timeout: float = 5.0) -> sqlite3.Connection:
    """The one way into the store — and the one place the schema is brought up to date.

    `write=False` opens a reader, and a reader changes nothing: no store is
    created, no column is added, no index is built. That is not tidiness — a
    status line renders inside someone's shell prompt, and a prompt is the
    worst possible place to spend half a second building an index, take a write
    lock two shells are racing for, or discover the store is on read-only media.
    A reader that finds a schema older than this build says so and leaves it for
    the next command that legitimately writes.

    `timeout` bounds the wait for a writer's lock, for readers that cannot
    afford to block.
    """
    if not write:
        if not path.exists():
            raise FileNotFoundError(path)
        con = sqlite3.connect(path, timeout=timeout)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = 1")
        have = {r["name"] for r in con.execute("PRAGMA table_info(events)")}
        stale = [col for col, _ in MIGRATIONS if col not in have]
        if have and stale:
            raise RuntimeError(f"store predates this build (no {', '.join(stale)}); "
                               f"any command that writes will migrate it")
        return con
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=timeout)
    con.row_factory = sqlite3.Row
    # migrate before the schema script runs: its indexes reference newer columns
    have = {r["name"] for r in con.execute("PRAGMA table_info(events)")}
    if have:
        for col, ddl in MIGRATIONS:
            if col not in have:
                con.execute(ddl)
        con.commit()
    con.executescript(SCHEMA)
    return con


def cmd_ingest(args) -> int:
    con = connect(Path(args.db).expanduser() if args.db else DB_PATH)
    cols = (
        "id ts day host source provider billing plan account model project repo session "
        "input output cache_read cache_write cache_write_1h cash_usd"
    ).split()
    # Re-sending an event is a no-op except for one case: an event stored before
    # accounts existed gets its account filled in. Nothing else is ever rewritten.
    sql = (f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
           f"ON CONFLICT(id) DO UPDATE SET account = excluded.account "
           f"WHERE events.account = '' AND excluded.account <> ''")
    seen = new = 0
    batch = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev["day"] = ev["ts"][:10]
        ev["account"] = ev.get("account") or ""  # events from a pre-account collector
        batch.append([ev.get(c) for c in cols])
        seen += 1
        if len(batch) >= 5000:
            new += con.executemany(sql, batch).rowcount
            con.commit()
            batch = []
    if batch:
        new += con.executemany(sql, batch).rowcount
    con.execute(
        "INSERT INTO meta(k,v) VALUES('last_ingest',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
    )
    con.commit()
    print(f"tokentab ingest: {seen} received, {new} written (new or account backfilled), "
          f"{seen - new} unchanged", file=sys.stderr)
    return 0


def cmd_push(args) -> int:
    server = args.server
    scan = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "scan"] + (["--all"] if args.all else []),
        stdout=subprocess.PIPE,
    )
    ssh = subprocess.Popen(["ssh", server, args.remote_cmd, "ingest"], stdin=scan.stdout)
    scan.stdout.close()
    rc = ssh.wait()
    scan.wait()
    return rc


def cmd_adopt(args) -> int:
    """Stamp an account onto flat-plan rows that predate account tracking.

    A re-scan can only stamp events whose transcript still exists, and vendors
    prune transcripts — so history would keep an empty account and show up as a
    phantom account-less subscription paying a full fee. Each (host, source)
    that has exactly one known account adopts its orphans; anything ambiguous is
    reported and left alone rather than guessed. Dry-run unless --apply.
    """
    con = connect(Path(args.db).expanduser() if args.db else DB_PATH)
    rows = con.execute(
        "SELECT host, source, account, COUNT(*) n FROM events "
        "WHERE plan IS NOT NULL AND plan <> '' GROUP BY host, source, account"
    ).fetchall()
    known: dict[tuple, set] = {}
    orphans: dict[tuple, int] = {}
    for r in rows:
        key = (r["host"], r["source"])
        if r["account"]:
            known.setdefault(key, set()).add(r["account"])
        else:
            orphans[key] = orphans.get(key, 0) + r["n"]
    if not orphans:
        print("adopt: no account-less flat-plan events — nothing to do")
        return 0
    total = 0
    for key, n in sorted(orphans.items()):
        host, source = key
        accts = known.get(key, set())
        if args.set:
            target, why = args.set, "forced by --set"
        elif len(accts) == 1:
            target, why = next(iter(accts)), "only account seen on this host+source"
        else:
            print(f"  {host:<12} {source:<12} {n:>8,} orphans   SKIP — "
                  + (f"{len(accts)} accounts seen: {', '.join(sorted(accts))}" if accts
                     else "no account ever seen here") + "; pass --set to decide")
            continue
        print(f"  {host:<12} {source:<12} {n:>8,} orphans -> {target}   ({why})")
        if args.apply:
            con.execute(
                "UPDATE events SET account = ? WHERE account = '' AND host = ? AND source = ? "
                "AND plan IS NOT NULL AND plan <> ''", (target, host, source))
        total += n
    if args.apply:
        con.commit()
        print(f"adopt: {total:,} events stamped")
    else:
        print(f"adopt: {total:,} events would be stamped — re-run with --apply")
    return 0


# ---------------------------------------------------------------- pricing


def rate_for(model: str, rates: dict, provider: str | None = None) -> dict:
    models = rates["models"]
    aliases = rates.get("aliases", {})
    m = aliases.get(model, model)
    if m in rates.get("free_models", []):
        return {k: 0.0 for k in ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")}
    if m in models:
        return models[m]
    best = None
    for k in models:
        if m.startswith(k) and (best is None or len(k) > len(best)):
            best = k
    if best:
        return models[best]
    return rates["local_fallback"] if provider == "local" else rates["fallback"]


def value_of(row, rates: dict) -> float:
    r = rate_for(row["model"], rates, row["provider"] if "provider" in row.keys() else None)
    return (
        row["input"] * r["input"]
        + row["output"] * r["output"]
        + row["cache_read"] * r["cache_read"]
        + row["cache_write"] * r["cache_write_5m"]
        + row["cache_write_1h"] * r["cache_write_1h"]
    ) / 1_000_000


# ---------------------------------------------------------------- billing cycles


def cycle_bounds(day: date, cycle_day: int) -> tuple[date, date]:
    """The [start, end) plan cycle containing `day`, renewing on `cycle_day`."""

    def anchor(y: int, m: int) -> date:
        return date(y, m, min(cycle_day, calendar.monthrange(y, m)[1]))

    a = anchor(day.year, day.month)
    if day >= a:
        start = a
        ny, nm = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        end = anchor(ny, nm)
    else:
        py, pm = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
        start = anchor(py, pm)
        end = a
    return start, end


def cycles_overlapping(frm: date, to: date, cycle_day: int) -> list[tuple[date, date]]:
    out = []
    start, end = cycle_bounds(frm, cycle_day)
    while start < to:
        out.append((start, end))
        start = end
        end = cycle_bounds(end, cycle_day)[1]
        if len(out) > 400:
            break
    return out


def resolve_period(preset: str, frm: str | None, to: str | None, plans: dict) -> dict:
    today = datetime.now(timezone.utc).date()
    primary = next(iter(plans["plans"].values()), {"cycle_day": 1})
    cday = int(primary.get("cycle_day", 1))
    if preset == "custom" and frm and to:
        f, t = date.fromisoformat(frm), date.fromisoformat(to) + timedelta(days=1)
    elif preset == "prev_cycle":
        s, _ = cycle_bounds(today, cday)
        f, t = cycle_bounds(s - timedelta(days=1), cday)
    elif preset == "7d":
        f, t = today - timedelta(days=6), today + timedelta(days=1)
    elif preset == "30d":
        f, t = today - timedelta(days=29), today + timedelta(days=1)
    elif preset == "mtd":
        f, t = today.replace(day=1), today + timedelta(days=1)
    elif preset == "all":
        f, t = date(2000, 1, 1), today + timedelta(days=1)
    else:
        preset = "cycle"
        f, t = cycle_bounds(today, cday)
        t = min(t, today + timedelta(days=1))
    return {"preset": preset, "from": f.isoformat(), "to": (t - timedelta(days=1)).isoformat(),
            "_from": f, "_to": t}


# ---------------------------------------------------------------- query


FILTER_COLS = {"provider": "provider", "billing": "billing", "project": "project",
               "repo": "repo", "machine": "host", "model": "model", "source": "source",
               "plan": "plan", "account": "account"}


def plan_instances(con, plans: dict) -> list[dict]:
    """One subscription per (plan family × account), discovered from the data.

    A plan family in plans.json is a *template*, not a subscription: the number
    of accounts changes over time, so instances are built from the (plan,
    account) pairs actually present in the store. An account nobody has
    configured still bills — at the family's default fee, flagged so the number
    is never silently wrong.
    """
    cfg_accounts = plans.get("accounts", {})
    seen = con.execute(
        "SELECT plan, account, MIN(day) first_day, MAX(day) last_day FROM events "
        "WHERE plan IS NOT NULL AND plan <> '' GROUP BY plan, account"
    ).fetchall()
    out = []
    for r in seen:
        fam = plans["plans"].get(r["plan"])
        if not fam:
            continue
        acct = r["account"] or ""
        cfg = dict(cfg_accounts.get(acct, {}))
        active_to = cfg.get("active_to", fam.get("active_to"))
        inferred = False
        if active_to == "auto":
            # Cancelled, date unknown: the subscription is assumed to have ended
            # after the last usage it ever billed. Stated in the UI as inferred.
            active_to = (date.fromisoformat(r["last_day"]) + timedelta(days=1)).isoformat()
            inferred = True
        label = fam.get("label", r["plan"])
        if acct:
            label += f" · {cfg.get('label') or acct}"
        out.append({
            "id": f"{r['plan']}@{acct}" if acct else r["plan"],
            "plan": r["plan"],
            "account": acct,
            "label": label,
            "provider": fam["provider"],
            "monthly_usd": float(cfg.get("monthly_usd", fam["monthly_usd"])),
            "cycle_day": int(cfg.get("cycle_day", fam.get("cycle_day", 1))),
            "active_from": cfg.get("active_from") or fam.get("active_from") or "2000-01-01",
            "active_to": active_to,
            "inferred_end": inferred,
            "configured": acct in cfg_accounts,
            "first_day": r["first_day"],
            "last_day": r["last_day"],
        })
    return sorted(out, key=lambda p: (p["plan"], p["account"]))


def active_cycles(inst: dict, frm: date, to: date):
    """The billing cycles of one subscription that overlap [frm, to).

    A subscription bills for a whole cycle or not at all — it is never
    pro-rated — so this is also the count of fees owed in the window. One
    definition, because both the report and the status line total cash from it.
    """
    active_from = date.fromisoformat(inst["active_from"])
    active_to = date.fromisoformat(inst["active_to"]) if inst["active_to"] else None
    for cs, ce in cycles_overlapping(frm, to, inst["cycle_day"]):
        if ce <= active_from or (active_to and cs >= active_to):
            continue
        yield cs, ce


def where_from(filters: dict, extra_sql: str = "", params: list | None = None):
    sql, args = [], list(params or [])
    for key, col in FILTER_COLS.items():
        v = filters.get(key)
        if v:
            sql.append(f"{col} = ?")
            args.append(v)
    if extra_sql:
        sql.append(extra_sql)
    return (" AND ".join(sql) or "1=1"), args


def summarise(con, rates: dict, plans: dict, period: dict, filters: dict) -> dict:
    frm, to = period["_from"].isoformat(), period["_to"].isoformat()
    win = "day >= ? AND day < ?"
    where, args = where_from(filters, win, [])
    args = args + [frm, to]

    def agg(group: str | None):
        # provider always rides along: it decides which fallback rate an unknown
        # (local) model is priced at.
        keys = [k for k in (group, "model", "provider") if k]
        keys = list(dict.fromkeys(keys))
        cols = ", ".join(keys)
        q = (f"SELECT {cols}, SUM(input) input, SUM(output) output, "
             f"SUM(cache_read) cache_read, SUM(cache_write) cache_write, "
             f"SUM(cache_write_1h) cache_write_1h, SUM(cash_usd) cash, COUNT(*) n "
             f"FROM events WHERE {where} GROUP BY {cols}")
        return con.execute(q, args).fetchall()

    def rollup(group: str) -> list[dict]:
        acc: dict[str, dict] = {}
        for r in agg(group):
            k = r[group] or "—"
            a = acc.setdefault(k, {"key": k, "value": 0.0, "cash": 0.0, "tokens": 0, "events": 0})
            a["value"] += value_of(r, rates)
            a["cash"] += r["cash"] or 0.0
            a["tokens"] += sum(r[c] for c in TOKEN_COLS)
            a["events"] += r["n"]
        return sorted(acc.values(), key=lambda x: -x["value"])

    total = {"value": 0.0, "cash_metered": 0.0, "tokens": 0, "events": 0}
    for r in agg(None):
        total["value"] += value_of(r, rates)
        total["cash_metered"] += r["cash"] or 0.0
        total["tokens"] += sum(r[c] for c in TOKEN_COLS)
        total["events"] += r["n"]

    # --- flat-plan allocation, per plan per billing cycle -------------------
    allocated = 0.0
    plan_rows, notes = [], []
    partial = False
    unconfigured, inferred_ends = [], []
    for inst in plan_instances(con, plans):
        if filters.get("plan") and filters["plan"] != inst["plan"]:
            continue
        if filters.get("account") and filters["account"] != inst["account"]:
            continue
        if filters.get("provider") and filters["provider"] != inst["provider"]:
            continue
        if filters.get("billing") and filters["billing"] != "flat":
            continue
        fee = inst["monthly_usd"]
        p_alloc = p_cash = 0.0
        for cs, ce in active_cycles(inst, period["_from"], period["_to"]):
            p_cash += fee
            if cs < period["_from"] or ce > period["_to"]:
                partial = True
            wf = max(cs, period["_from"]).isoformat()
            wt = min(ce, period["_to"]).isoformat()
            denom = con.execute(
                "SELECT SUM(input+output+cache_read+cache_write+cache_write_1h) t "
                "FROM events WHERE plan = ? AND account = ? AND day >= ? AND day < ?",
                (inst["plan"], inst["account"], cs.isoformat(), ce.isoformat()),
            ).fetchone()["t"] or 0
            if not denom:
                continue
            fw, fa = where_from({k: v for k, v in filters.items() if k not in ("plan", "account")},
                                "plan = ? AND account = ? AND day >= ? AND day < ?", [])
            numer = con.execute(
                f"SELECT SUM(input+output+cache_read+cache_write+cache_write_1h) t "
                f"FROM events WHERE {fw}", fa + [inst["plan"], inst["account"], wf, wt]
            ).fetchone()["t"] or 0
            p_alloc += fee * (numer / denom)
        if p_cash or p_alloc:
            plan_rows.append({"key": inst["label"], "plan": inst["plan"],
                              "account": inst["account"], "fee": fee,
                              "ended": inst["active_to"], "inferred_end": inst["inferred_end"],
                              "cash": round(p_cash, 2), "allocated": round(p_alloc, 2)})
            if not inst["configured"] and inst["account"]:
                unconfigured.append(inst["account"])
            if inst["inferred_end"]:
                inferred_ends.append(f"{inst['label']} (to {inst['active_to']})")
        allocated += p_alloc
    cash_plans = sum(r["cash"] for r in plan_rows)

    # --- headline ----------------------------------------------------------
    narrowed = any(filters.get(k) for k in ("project", "repo", "model", "machine", "source"))
    if narrowed:
        headline_cash, cash_kind = allocated + total["cash_metered"], "allocated"
        notes.append("A slice of a flat plan has no cash figure of its own — the headline shows "
                     "the ALLOCATED share (plan fee × token share) plus any metered spend.")
    else:
        headline_cash, cash_kind = cash_plans + total["cash_metered"], "cash"
    if unconfigured:
        notes.append("No fee configured for " + ", ".join(sorted(set(unconfigured))) +
                     " — billed at the plan's default rate. Add the account to plans.json to "
                     "correct it.")
    if inferred_ends:
        notes.append("End date inferred from last usage (not from an invoice): " +
                     ", ".join(sorted(set(inferred_ends))) + ".")
    if partial:
        notes.append("The window covers only part of a billing cycle: cash shows the full cycle "
                     "fee (plans are not pro-rated), allocated counts only usage inside the window.")
    if any(rate_for(m, rates).get("reference") for m in
           [r["key"] for r in rollup("model")]):
        notes.append("Local models have no list price — their Value uses a reference rate for a "
                     "comparable hosted model (see rates.json).")
    if con.execute(f"SELECT 1 FROM events WHERE {where} AND source='llama_swap' LIMIT 1",
                   args).fetchone():
        notes.append("Local (llama-swap) usage is day-resolution and not project-attributed: the "
                     "proxy sees no working directory.")

    return {
        "period": {k: period[k] for k in ("preset", "from", "to")},
        "filters": {k: v for k, v in filters.items() if v},
        "headline": {
            "cash_usd": round(headline_cash, 2),
            "cash_kind": cash_kind,
            "allocated_usd": round(allocated, 2),
            "value_usd": round(total["value"], 2),
            "tokens": total["tokens"],
            "events": total["events"],
        },
        "plans": plan_rows,
        "breakdown": {g: rollup(FILTER_COLS[g]) for g in
                      ("project", "provider", "model", "machine", "billing", "repo", "source",
                       "account")},
        "daily": daily(con, rates, where, args),
        "notes": notes,
    }


def daily(con, rates, where, args) -> list[dict]:
    q = (f"SELECT day, model, provider, SUM(input) input, SUM(output) output, "
         f"SUM(cache_read) cache_read, SUM(cache_write) cache_write, "
         f"SUM(cache_write_1h) cache_write_1h, SUM(cash_usd) cash, COUNT(*) n "
         f"FROM events WHERE {where} GROUP BY day, model, provider ORDER BY day")
    acc: dict[str, dict] = {}
    for r in con.execute(q, args):
        a = acc.setdefault(r["day"], {"day": r["day"], "value": 0.0, "tokens": 0})
        a["value"] += value_of(r, rates)
        a["tokens"] += sum(r[c] for c in TOKEN_COLS)
    return [{"day": k, "value": round(v["value"], 4), "tokens": v["tokens"]}
            for k, v in sorted(acc.items())]


def distinct_values(con) -> dict:
    out = {}
    for key, col in FILTER_COLS.items():
        rows = con.execute(
            f"SELECT DISTINCT {col} v FROM events WHERE {col} IS NOT NULL AND {col} <> '' "
            f"ORDER BY 1"
        ).fetchall()
        out[key] = [r["v"] for r in rows]
    return out


# ---------------------------------------------------------------- dashboard


# The dashboard is a built React SPA (web/) consuming the JSON API below. The
# build is a static bundle — no Node runtime on the serving host, just files.
WEB_DIR = Path(os.environ.get("TOKENTAB_WEB", str(HERE / "web" / "dist"))).expanduser()

NO_BUILD_HTML = b"""<!doctype html><meta charset="utf-8"><title>tokentab</title>
<body style="font:14px ui-monospace,monospace;padding:2rem">
<p>UI bundle not found at <code>%s</code>.</p>
<p>Build it with <code>cd web &amp;&amp; npm ci &amp;&amp; npm run build</code>, or deploy
<code>web/dist/</code> alongside <code>tokentab.py</code>.</p>
<p>The JSON API is unaffected: <a href="api/summary">api/summary</a></p>"""

CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".json": "application/json",
          ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
          ".woff": "font/woff", ".woff2": "font/woff2"}


class Handler(BaseHTTPRequestHandler):
    server_version = "tokentab"

    def _send(self, code, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _send_asset(self, path: str) -> None:
        """Serve one file out of the built bundle, or the SPA shell for any
        unknown path. Everything under the bundle is content-hashed by the
        build except index.html, which must never be cached or a deploy is
        invisible until a hard reload."""
        rel = path.lstrip("/") or "index.html"
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            target = WEB_DIR / "index.html"  # client-side routing / unknown path
        if not target.is_file():
            return self._send(200, NO_BUILD_HTML % str(WEB_DIR).encode(),
                              "text/html; charset=utf-8")
        cache = "no-store" if target.name == "index.html" else "public, max-age=31536000, immutable"
        self._send(200, target.read_bytes(),
                   CTYPES.get(target.suffix, "application/octet-stream"), cache)

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/api/filters":
                return self._send(200, json.dumps(distinct_values(self.server.get_con())).encode(),
                                  "application/json")
            if u.path == "/api/summary":
                cfg = self.server.cfg
                period = resolve_period(q.get("preset", "cycle"), q.get("from"), q.get("to"),
                                        cfg["plans"])
                out = summarise(self.server.get_con(), cfg["rates"], cfg["plans"], period,
                                {k: q.get(k) for k in FILTER_COLS})
                row = self.server.get_con().execute("SELECT v FROM meta WHERE k='last_ingest'").fetchone()
                out["updated"] = row["v"][:16].replace("T", " ") + " UTC" if row else "never"
                return self._send(200, json.dumps(out).encode(), "application/json")
            if u.path.startswith("/api/"):
                return self._send(404, b'{"error":"no such endpoint"}', "application/json")
            self._send_asset(u.path)
        except Exception as e:  # keep the dashboard up, surface the error
            self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")

    def log_message(self, *a):  # quiet
        pass


def cmd_serve(args) -> int:
    db = Path(args.db).expanduser() if args.db else DB_PATH
    connect(db).close()  # ensure the schema exists before serving
    local = threading.local()

    def get_con():
        # sqlite connections are not shareable across threads, and the server is
        # threaded — one read-only connection per worker thread.
        if not getattr(local, "con", None):
            local.con = sqlite3.connect(db, check_same_thread=False)
            local.con.row_factory = sqlite3.Row
            local.con.execute("PRAGMA query_only = 1")
        return local.con

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.get_con = get_con
    srv.cfg = {"rates": load_rates(), "plans": load_plans()}
    print(f"tokentab serving on http://{args.bind}:{args.port}/", file=sys.stderr)
    srv.serve_forever()
    return 0


# ---------------------------------------------------------------- cli reports


def cmd_report(args) -> int:
    con = connect(Path(args.db).expanduser() if args.db else DB_PATH)
    rates, plans = load_rates(), load_plans()
    period = resolve_period(args.preset, args.frm, args.to, plans)
    filters = {k: getattr(args, k, None) for k in FILTER_COLS}
    d = summarise(con, rates, plans, period, filters)
    if args.json:
        json.dump(d, sys.stdout, indent=2)
        print()
        return 0
    h = d["headline"]
    print(f"\n  tokentab · {d['period']['from']} → {d['period']['to']}  ({d['period']['preset']})")
    if d["filters"]:
        print("  filters: " + ", ".join(f"{k}={v}" for k, v in d["filters"].items()))
    label = "Allocated (no cash figure for a slice)" if h["cash_kind"] == "allocated" else "Cash out"
    print(f"\n  {label:<42} ${h['cash_usd']:>10,.2f}")
    print(f"  {'Allocated cost':<42} ${h['allocated_usd']:>10,.2f}")
    print(f"  {'Value at list rates':<42} ${h['value_usd']:>10,.2f}")
    print(f"  {'Tokens':<42} {h['tokens']:>11,}  ({h['events']:,} calls)")
    if d["plans"]:
        print("\n  subscriptions")
        for r in d["plans"]:
            end = f"   ended {r['ended']}{'?' if r['inferred_end'] else ''}" if r["ended"] else ""
            print(f"    {r['key'][:34]:<34} fee ${r['fee']:>7,.2f}   cash ${r['cash']:>9,.2f}"
                  f"   alloc ${r['allocated']:>9,.2f}{end}")
    for dim in ("project", "account", "provider", "model", "machine"):
        rows = d["breakdown"][dim][:10]
        if not rows:
            continue
        print(f"\n  by {dim}")
        for r in rows:
            print(f"    {r['key'][:34]:<34} ${r['value']:>9,.2f}   {r['tokens']:>13,} tok")
    if d["notes"]:
        print()
        for n in d["notes"]:
            print(f"  ! {n}")
    print()
    return 0


def statusline_numbers(con, rates: dict, plans: dict) -> dict:
    """Today and the current cycle — the three numbers a status line needs.

    `summarise` answers the same question, but to do it builds nine breakdown
    rollups and a daily series: a second of work on a large store, repeated on
    every prompt render. This asks for the three numbers instead, in one query
    plus the plan discovery.

    Cash is the *full* fee of every cycle the window overlaps — plans are not
    pro-rated, and totalling them needs no allocation. The rule is the one in
    `summarise`; keep the two in step (`tokentab verify` checks that they are).
    """
    period = resolve_period("cycle", None, None, plans)
    frm, to = period["_from"], period["_to"]
    today = datetime.now(timezone.utc).date().isoformat()
    out = {"today": 0.0, "value": 0.0, "cash": 0.0}
    for r in con.execute(
        "SELECT day, model, provider, SUM(input) input, SUM(output) output, "
        "SUM(cache_read) cache_read, SUM(cache_write) cache_write, "
        "SUM(cache_write_1h) cache_write_1h, SUM(cash_usd) cash "
        "FROM events WHERE day >= ? AND day < ? GROUP BY day, model, provider",
        (frm.isoformat(), to.isoformat()),
    ):
        v = value_of(r, rates)
        out["value"] += v
        out["cash"] += r["cash"] or 0.0
        if r["day"] == today:
            out["today"] += v
    for inst in plan_instances(con, plans):
        for _ in active_cycles(inst, frm, to):
            out["cash"] += inst["monthly_usd"]
    return out


def cmd_statusline(args) -> int:
    """One line of current spend, for a shell prompt or an agent's status bar.

    Always the current billing cycle. Other windows would put a number on the
    line that cannot be read without its caveat — a cycle's cash is the whole
    fee however little of the cycle the window covers, so `value of cash` over
    any other span compares a slice against a full month. `report --json` is
    the way to ask about those.

    Nothing is printed unless the whole line can be printed. A status line is
    embedded in someone's prompt, so a store that is missing, locked, or busy
    is a reason to say nothing, not to paint an error over their terminal on
    every render. Set TOKENTAB_DEBUG to see why the line is blank.
    """
    try:
        path = Path(args.db).expanduser() if args.db else DB_PATH
        # A reader, and briefly: rendering a prompt must not create a store at a
        # mistyped path, must not migrate or index one, and must not wait on one
        # `ingest` is mid-commit on.
        con = connect(path, write=False, timeout=0.2)
        n = statusline_numbers(con, load_rates(), load_plans())
    except Exception as exc:  # never break the prompt
        if os.environ.get("TOKENTAB_DEBUG"):
            print(f"tokentab statusline: {exc}", file=sys.stderr)
        return 0
    parts = [f"${n['today']:,.2f} today", f"${n['value']:,.2f} of ${n['cash']:,.2f}"]
    if n["cash"] > 0:
        parts.append(f"{n['value'] / n['cash']:.2f}×")
    print(" · ".join(parts))
    return 0


def cmd_verify(args) -> int:
    """Acceptance checks — allocation conservation and a rate spot-check."""
    con = connect(Path(args.db).expanduser() if args.db else DB_PATH)
    rates, plans = load_rates(), load_plans()
    ok = True

    # 1. Conservation: over a whole billing cycle, per-project allocations must
    #    sum back to the plan fee (to the cent).
    print("conservation — allocated sums to cash out, over a whole cycle")
    period = resolve_period("prev_cycle", None, None, plans)
    total = summarise(con, rates, plans, period, {k: None for k in FILTER_COLS})
    per_project = 0.0
    projects = [r["key"] for r in total["breakdown"]["project"]]
    for p in projects:
        f = {k: None for k in FILTER_COLS}
        f["project"] = p
        per_project += summarise(con, rates, plans, period, f)["headline"]["allocated_usd"]
    cash = sum(r["cash"] for r in total["plans"])
    allocatable = sum(r["allocated"] for r in total["plans"])
    drift = abs(per_project - allocatable)
    tol = max(0.01 * len(projects), 0.05)
    print(f"  cycle {period['from']} → {period['to']}")
    print(f"  plan fees        ${cash:,.2f}")
    print(f"  allocatable      ${allocatable:,.2f}"
          + (f"   (${cash - allocatable:,.2f} paid but unused — no usage to allocate it to)"
             if cash - allocatable > 0.005 else ""))
    print(f"  Σ per-project    ${per_project:,.2f}   drift ${drift:,.4f} (tol ${tol:,.2f})")
    if not projects:
        print("  SKIP — no usage in the previous cycle")
    elif drift > tol:
        ok = False
        print("  FAIL")
    else:
        print("  PASS")

    # 1b. The status line totals cash and value its own cheap way, to stay fast
    #     enough to run on every prompt. It must still say what report says.
    print("\nstatusline agrees with report — same cycle, same numbers")
    cyc = resolve_period("cycle", None, None, plans)
    rep = summarise(con, rates, plans, cyc, {k: None for k in FILTER_COLS})["headline"]
    sl = statusline_numbers(con, rates, plans)
    for label, a, b in (("cash", sl["cash"], rep["cash_usd"]),
                        ("value", sl["value"], rep["value_usd"])):
        flag = "PASS" if abs(a - b) < 0.01 else "FAIL"
        ok &= flag == "PASS"
        print(f"  {label:<18} statusline ${a:>10,.2f}   report ${b:>10,.2f}   {flag}")

    # 2. Rate spot-check: a hand-computed price for a known model.
    print("\nrate spot-check — 1M input + 1M output at list price")
    for model, want in (("claude-opus-5", 5.0 + 25.0), ("claude-sonnet-5", 2.0 + 10.0),
                        ("gpt-5.6-terra", 2.0 + 12.0)):
        row = {"model": model, "input": 1_000_000, "output": 1_000_000,
               "cache_read": 0, "cache_write": 0, "cache_write_1h": 0}
        got = value_of(row, rates)
        flag = "PASS" if abs(got - want) < 1e-6 else "FAIL"
        ok &= flag == "PASS"
        print(f"  {model:<18} expected ${want:>7,.2f}   got ${got:>7,.2f}   {flag}")

    # 3. Coverage: which sources and hosts have landed.
    print("\ncoverage")
    for r in con.execute("SELECT host, source, COUNT(*) n, MIN(day) a, MAX(day) b "
                         "FROM events GROUP BY host, source ORDER BY host, source"):
        print(f"  {r['host']:<12} {r['source']:<12} {r['n']:>8,} events   {r['a']} → {r['b']}")
    print("\nsubscriptions (plan × account, discovered from the data)")
    for inst in plan_instances(con, plans):
        end = f"→ {inst['active_to']}{'?' if inst['inferred_end'] else ''}" if inst["active_to"] \
            else "→ active"
        print(f"  {inst['label'][:34]:<34} ${inst['monthly_usd']:>7,.2f}/mo  day {inst['cycle_day']:<2}"
              f"  {inst['first_day']} → {inst['last_day']}  {end}"
              + ("" if inst["configured"] or not inst["account"] else "   ! fee not configured"))
    orphan = con.execute("SELECT COUNT(*) n FROM events WHERE account = '' "
                         "AND plan IS NOT NULL AND plan <> ''").fetchone()["n"]
    if orphan:
        ok = False
        print(f"\n  ! {orphan:,} flat-plan events carry no account — they bill as a phantom "
              f"account-less subscription. Run: tokentab adopt --apply")

    unknown = con.execute(
        "SELECT model, provider, COUNT(*) n FROM events GROUP BY model, provider "
        "ORDER BY n DESC").fetchall()
    missing = [r["model"] for r in unknown
               if rate_for(r["model"], rates, r["provider"]) is rates["fallback"]]
    if missing:
        print(f"\n  ! no published rate for: {', '.join(missing)} — priced at the fallback rate")
    local = con.execute(
        "SELECT model, SUM(input+output) t FROM events WHERE provider='local' "
        "GROUP BY model ORDER BY t DESC").fetchall()
    if local:
        print("\n  local models (no list price — Value uses a reference rate):")
        for r in local:
            print(f"    {r['model']:<34} {r['t']:>13,} tok")
    print(f"\n{'ALL CHECKS PASSED' if ok else 'CHECKS FAILED'}")
    return 0 if ok else 1


# ---------------------------------------------------------------- main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tokentab", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="emit NDJSON usage events from local transcripts")
    s.add_argument("--all", action="store_true", help="ignore saved offsets (full backfill)")
    s.add_argument("--dry-run", action="store_true", help="do not persist scan offsets")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("backfill", help="alias for scan --all")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_scan, all=True)

    s = sub.add_parser("push", help="scan and pipe into the server's ingest")
    # No default host: which machine collects is deployment-specific, and a
    # baked-in name would silently push somewhere wrong. Set TOKENTAB_SERVER
    # in the unit that runs this.
    s.add_argument("--server", default=os.environ.get("TOKENTAB_SERVER"), required=not
                   os.environ.get("TOKENTAB_SERVER"), help="ssh host running `tokentab ingest`")
    # relative to the remote home dir — a non-interactive ssh shell may not have
    # ~/.local/bin on PATH, and a local `~` would expand on the wrong machine
    s.add_argument("--remote-cmd", default=os.environ.get("TOKENTAB_REMOTE", ".local/bin/tokentab"))
    s.add_argument("--all", action="store_true")
    s.set_defaults(func=cmd_push)

    s = sub.add_parser("ingest", help="read NDJSON events on stdin into the store")
    s.add_argument("--db")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("adopt", help="stamp accounts onto events that predate account tracking")
    s.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    s.add_argument("--set", help="account to use where the host+source is ambiguous")
    s.add_argument("--db")
    s.set_defaults(func=cmd_adopt)

    s = sub.add_parser("serve", help="run the dashboard")
    s.add_argument("--bind", default=os.environ.get("TOKENTAB_BIND", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("TOKENTAB_PORT", "8899")))
    s.add_argument("--db")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("report", help="CLI summary")
    s.add_argument("--preset", default="cycle",
                   choices=["cycle", "prev_cycle", "7d", "30d", "mtd", "all", "custom"])
    s.add_argument("--from", dest="frm")
    s.add_argument("--to")
    for k in FILTER_COLS:
        s.add_argument(f"--{k}")
    s.add_argument("--json", action="store_true", help="emit the summary as JSON instead of a table")
    s.add_argument("--db")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("statusline", help="one line of this cycle's spend, for a prompt or status bar")
    s.add_argument("--db")
    s.set_defaults(func=cmd_statusline)

    s = sub.add_parser("verify", help="acceptance checks")
    s.add_argument("--db")
    s.set_defaults(func=cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
