#!/usr/bin/env python3
"""Generate the static demo fixture the public dashboard runs on.

The demo has no server and no database, so every answer it can give has to be
precomputed. This builds a synthetic store — invented accounts, projects and
machines, no real data of any kind — and then asks *the real engine*
(`summarise` in tokentab.py) for one summary per view the demo can reach:
every preset, alone and with each single filter applied.

Precomputing with the real engine rather than reimplementing the maths in
TypeScript is the whole point: the demo shows what the product would actually
say, and there is no second copy of the allocation rules to drift.

    python3 tools/make-demo.py [-o web/public/demo.json]

Combinations of two or more filters are not precomputed — the matrix squares.
The web app drops the extra filters and says so (see web/src/lib/api.ts).
"""

import argparse
import json
import random
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import tokentab  # noqa: E402

# ---------------------------------------------------------------- the world

# Everything below is invented. Names are deliberately generic so nothing here
# can be mistaken for — or grepped back to — a real host, account or project.

PLANS = {
    "plans": {
        "claude-max": {
            "label": "Claude Max",
            "provider": "anthropic",
            "monthly_usd": 200.0,
            "cycle_day": 7,
            "active_from": "2026-01-01",
            "active_to": None,
            "sources": ["claude_code"],
        },
        "chatgpt": {
            "label": "ChatGPT (Codex)",
            "provider": "openai",
            "monthly_usd": 20.0,
            "cycle_day": 1,
            "active_from": "2026-01-01",
            "active_to": None,
            "sources": ["codex"],
        },
    },
    "accounts": {
        "ada@example.com": {"label": "personal"},
        "grace@example.work": {"label": "work"},
        # Cancelled on a date nobody wrote down — the end is inferred from the
        # last usage and rendered with a "?" so the demo also shows that path.
        "pat@example.com": {"label": "cancelled", "active_to": "auto"},
    },
    "hosts": {"nimbus": "NIMBUS", "foundry": "FOUNDRY", "atoll": "ATOLL"},
    "roots": {"paths": ["~/code"]},
}

# machine -> what runs on it. The mix is what makes the demo readable: a laptop
# on a personal plan, a workstation with local models, a server that also runs
# a metered API key.
MACHINES = {
    "nimbus": [("claude_code", "ada@example.com"), ("codex", "ada@example.com")],
    "foundry": [
        ("claude_code", "grace@example.work"),
        ("codex", "grace@example.work"),
        ("llama_swap", ""),
    ],
    "atoll": [("claude_code", "pat@example.com"), ("llama_swap", "")],
}

# project -> repos. Weight decides how much of the fleet's tokens it takes.
PROJECTS = {
    "atlas": (["atlas-api", "atlas-web"], 30),
    "beacon": (["beacon-app"], 22),
    "citadel": (["citadel-core", "citadel-cli"], 18),
    "driftwood": (["driftwood-www"], 12),
    "meridian": (["meridian-docs"], 8),
    "ad-hoc": (["ad-hoc"], 10),
}

# source -> (provider, billing, plan, [models]). Metered rides on claude_code
# from the server, so the demo has a real cash figure that is not a plan fee.
SOURCES = {
    "claude_code": ("anthropic", "flat", "claude-max",
                    ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]),
    "codex": ("openai", "flat", "chatgpt", ["gpt-5.6-sol", "gpt-5.4-mini"]),
    "llama_swap": ("local", "local", None, ["qwen36", "gpt-oss-120b"]),
}

METERED = ("anthropic", "metered", None, ["claude-sonnet-5"])

DAYS = 80
PRESETS = ["cycle", "prev_cycle", "7d", "30d", "mtd", "all"]

# Dimensions the demo can filter on — the web app's DIMS, in the same order.
DIMS = ["account", "provider", "billing", "project", "repo", "machine", "model", "source"]


def build_store(con: sqlite3.Connection, today: date) -> None:
    rnd = random.Random(20260813)
    rows = []
    n = 0
    for offset in range(DAYS):
        day = today - timedelta(days=DAYS - 1 - offset)
        # A working week, and a fleet that grows a little over the window.
        intensity = (0.35 if day.weekday() >= 5 else 1.0) * (0.6 + 0.9 * offset / DAYS)
        for host, feeds in MACHINES.items():
            for source, account in feeds:
                # The cancelled account stops using anything 34 days back —
                # that is what `"active_to": "auto"` then infers from.
                if account == "pat@example.com" and (today - day).days < 34:
                    continue
                provider, billing, plan, models = SOURCES[source]
                for _ in range(rnd.randint(2, 5)):
                    project = rnd.choices(list(PROJECTS), [w for _, w in PROJECTS.values()])[0]
                    repo = rnd.choice(PROJECTS[project][0])
                    if source == "llama_swap":
                        project, repo = "ad-hoc", "ad-hoc"  # the proxy sees no cwd
                    model = rnd.choice(models)
                    calls = max(1, int(rnd.randint(10, 40) * intensity))
                    for _ in range(calls):
                        n += 1
                        scale = rnd.uniform(0.4, 2.2) * (3.5 if "opus" in model else 1.4)
                        rows.append((
                            f"demo-{n:07d}",
                            f"{day.isoformat()}T{rnd.randint(8, 22):02d}:"
                            f"{rnd.randint(0, 59):02d}:00+00:00",
                            day.isoformat(), host, source, provider, billing, plan, account,
                            model, project, repo, f"s{n // 40:05d}",
                            int(rnd.randint(400, 4000) * scale),
                            int(rnd.randint(300, 2600) * scale),
                            int(rnd.randint(8000, 90000) * scale),
                            int(rnd.randint(600, 9000) * scale),
                            0,
                            0.0,
                        ))
        # One metered batch a day from the server: real per-call cash, so the
        # demo's cash figure is not made only of flat fees.
        provider, billing, plan, models = METERED
        for _ in range(rnd.randint(3, 9)):
            n += 1
            inp, out = rnd.randint(2000, 30000), rnd.randint(1000, 9000)
            rows.append((
                f"demo-{n:07d}", f"{day.isoformat()}T{rnd.randint(8, 22):02d}:00:00+00:00",
                day.isoformat(), "atoll", "claude_code", provider, billing, plan, "",
                models[0], "beacon", "beacon-app", f"m{n // 40:05d}",
                inp, out, 0, 0, 0,
                round(inp * 3.0 / 1e6 + out * 15.0 / 1e6, 6),
            ))
    con.executemany(
        "INSERT INTO events (id, ts, day, host, source, provider, billing, plan, account, model, "
        "project, repo, session, input, output, cache_read, cache_write, cache_write_1h, cash_usd) "
        "VALUES (" + ",".join("?" * 19) + ")",
        rows,
    )
    con.execute("INSERT INTO meta(k,v) VALUES('last_ingest',?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    con.commit()


def key(preset: str, dim: str | None = None, value: str | None = None) -> str:
    """Fixture key. Mirrors demoKey() in web/src/lib/api.ts — keep them together."""
    return f"{preset}|{dim}={value}" if dim else f"{preset}|"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--out", default=str(HERE.parent / "web/public/demo.json"))
    args = ap.parse_args()

    # The demo bundle is committed, so it has to price from the repo's own
    # rates — not from whatever the person running this has installed. On a
    # developer's machine `_config_dir` finds ~/.config/tokentab and the numbers
    # come out of a table nobody reviewing the diff can see.
    tokentab.CONFIG_DIR = HERE.parent
    # load_rates, not a read of rates.json: it also loads price_history.json,
    # which is what prices a past day at the rate that applied then. Reading
    # the table alone would price 80 days of demo at today's rate and let a
    # broken history go through this check unnoticed.
    rates = tokentab.load_rates()
    today = datetime.now(timezone.utc).date()

    # The plans start when the data starts. Otherwise "All time" bills every
    # cycle back to active_from against 80 days of usage, and the demo opens on
    # a cash figure with nothing behind it.
    for fam in PLANS["plans"].values():
        fam["active_from"] = (today - timedelta(days=DAYS)).isoformat()

    with tempfile.TemporaryDirectory() as tmp:
        con = tokentab.connect(Path(tmp) / "demo.db")
        build_store(con, today)
        # The rows land with no Value, the way they do from any collector that
        # predates the column, and a report of a window holding one refuses to
        # answer. This is the same command an upgrading operator runs.
        tokentab.price_rows(con, rates)
        total = con.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        print(f"make-demo: {total:,} synthetic events over {DAYS} days", file=sys.stderr)

        options = tokentab.distinct_values(con)
        options.pop("plan", None)  # the UI filters by account, not by plan family
        updated = con.execute("SELECT v FROM meta WHERE k='last_ingest'").fetchone()["v"]

        views: list[tuple[str, dict]] = [(key(p), {}) for p in PRESETS]
        for p in PRESETS:
            for dim in DIMS:
                for v in options.get(dim, []):
                    views.append((key(p, dim, v), {dim: v}))

        summaries = {}
        for k, filt in views:
            preset = k.split("|", 1)[0]
            period = tokentab.resolve_period(preset, None, None, PLANS)
            out = tokentab.summarise(con, rates, PLANS, period,
                                     {c: filt.get(c) for c in tokentab.FILTER_COLS})
            out["updated"] = updated[:16].replace("T", " ") + " UTC"
            summaries[k] = out

        # Heartbeats, so the machines panel has all three of its states to show.
        # Invented like everything else here: one machine that checked in a
        # minute ago, one that has been quiet long enough to go red, and one
        # left out entirely — the shape of a collector too old to announce
        # itself.
        now = datetime.now(timezone.utc)
        for host, minutes in (("nimbus", 1), ("foundry", 9 * 60)):
            con.execute("INSERT INTO hosts(host,last_seen) VALUES(?,?) "
                        "ON CONFLICT(host) DO UPDATE SET last_seen=excluded.last_seen",
                        (host, (now - timedelta(minutes=minutes)).isoformat(timespec="seconds")))
        fleet = tokentab.machines(con)
        con.close()

    payload = {
        "_comment": "Synthetic demo data. Generated by tools/make-demo.py — no real accounts, "
                    "projects, machines or spend appear here.",
        "generated": today.isoformat(),
        "presets": PRESETS,
        "filters": options,
        "summaries": summaries,
        "machines": fleet,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"make-demo: {len(summaries)} precomputed views -> {out_path} "
          f"({out_path.stat().st_size / 1024:.0f} KB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
