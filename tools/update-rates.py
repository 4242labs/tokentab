#!/usr/bin/env python3
"""Check rates.json against LiteLLM's published price table, and update it.

The Value number is the whole reason tokentab exists — usage priced at list
rates — and it is only as good as rates.json. Hand-typed, that file goes stale
silently: a vendor cuts a price, nothing errors, and every historical Value is
quietly wrong. LiteLLM maintains the price table this reads instead.

    python3 tools/update-rates.py            # dry run — report drift, exit 1 if any
    python3 tools/update-rates.py --apply    # write rates.json
    python3 tools/update-rates.py --add gpt-5.7      # start pricing a new model

Two rules keep this from doing damage:

  * **Only a stated price is a price.** A vendor that does not charge to write a
    cache entry has no number to copy, and LiteLLM omits the field; reading that
    as $0 would silently rewrite those models' cache pricing. LiteLLM also
    writes a literal 0 for a price it does not know — 304 of its entries carry
    one — and that is not a free model either. A field is copied only when it
    carries a positive number.
  * **A price is never overwritten without being kept.** Events already stored
    carry the Value they were priced at, but a backfill, a re-scan or a
    `reprice` prices a past day from this file — so a rate cut today would
    otherwise re-price every day since tokentab started. The outgoing price is
    appended to price_history.json first, dated with `--as-of` or, failing that,
    the day this ran — which is not the day the vendor moved, so the lag is
    priced at the old rate. It is an add, never an edit, which is what makes
    this safe to run unattended.
  * **Local models are never touched.** Their rates are marked `reference` —
    deliberate stand-ins for a comparable hosted model, not quotes — and no
    upstream can have an opinion about them.

What `--apply` writes: the price object of every model it can quote, rewritten
in place; the `updated` date; and a `sources.litellm` entry if the file has none
yet. Nothing else — aliases, free models, the fallbacks and the comments are
left byte-for-byte. A rewritten price object does lose the file's hand-kept
column alignment, since it is regenerated rather than edited.

Stdlib only, like the rest of tokentab.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RATES = HERE.parent / "rates.json"   # the repo copy: rates.json is repo-owned, and
                                     # install.sh copies it out, so the repo wins
SOURCE = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
          "model_prices_and_context_window.json")

# Per million tokens, from LiteLLM's per-token fields. `cache_write_1h` falls
# back to the 5m write cost: only Anthropic prices a longer TTL separately, and
# where nobody does the two are the same number.
FIELDS = {
    "input": ("input_cost_per_token",),
    "output": ("output_cost_per_token",),
    "cache_write_5m": ("cache_creation_input_token_cost",),
    "cache_write_1h": ("cache_creation_input_token_cost_above_1hr",
                       "cache_creation_input_token_cost"),
    "cache_read": ("cache_read_input_token_cost",),
}

def tokentab_module():
    """tokentab.py as a module, or None if it cannot be had. Best effort, always.

    This is a maintenance tool for a file tokentab reads; asking tokentab where
    that file lives, and whether it can price what we wrote, beats re-deriving
    either answer here and letting the two drift. But nothing it tells us is
    worth taking the rates check down for, so every failure is just None.
    """
    src = HERE.parent / "tokentab.py"
    if not src.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_tokentab_for_rates", src)
        mod = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def in_use() -> Path | None:
    """The rates.json the installed tokentab prices from, if it is a different file.

    `install.sh` copies rates.json into XDG config, and `tokentab.py` reads that
    copy in preference to the repo — so editing the repo file changes nothing
    the CLI does until the next install. A tool whose whole point is catching a
    stale price file must not be the reason one goes unnoticed, so it says which
    file it is editing and which one is being priced from. Asking tokentab
    rather than re-deriving the rule keeps the two from drifting apart.
    """
    mod = tokentab_module()
    return mod and getattr(mod, "CONFIG_DIR", None) and mod.CONFIG_DIR / "rates.json"


def litellm_entry(name: str, table: dict) -> tuple[str, dict] | tuple[None, None]:
    """The LiteLLM record for one of our model names.

    Exact key first, then a dated variant — LiteLLM keeps
    `claude-opus-4-20250514` for models that shipped before the bare names
    existed. The date suffix is matched strictly so that `claude-opus-4` cannot
    swallow `claude-opus-4-1`.

    Only bare, first-party keys are considered, and the matching is what makes
    that true: both forms are anchored, so a key can differ from the model name
    by a date suffix and nothing else. LiteLLM carries the same models again
    under `anthropic.…`, `bedrock/…` and `azure/…` names at *those* platforms'
    prices, and every one of those keys is prefixed — none of them can match a
    bare tokentab model name. A model with no first-party quote is reported as
    hand-maintained instead.
    """
    if name in table:
        return name, table[name]
    dated = re.compile(rf"^{re.escape(name)}-\d{{8}}$")
    for key in sorted(table, reverse=True):  # newest dated build first
        if dated.match(key):
            return key, table[key]
    return None, None


def quoted_rate(entry: dict) -> dict:
    """The fields LiteLLM states, in tokentab's units. Anything else stays absent.

    "States" means a positive number that survives rounding to per-million. A
    0, a negative, a string, or a rate so small it rounds to nothing is not a
    quote — and a 0 written into rates.json does not read as a free model, it
    reads as a Value of $0.00 that never errors. Leaving the field absent keeps
    whatever rates.json already had, which is the safe direction.
    """
    out = {}
    for field, sources in FIELDS.items():
        for src in sources:
            v = entry.get(src)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                per_m = round(v * 1_000_000, 6)
                if per_m > 0:
                    out[field] = per_m
                    break
    return out


def num(v) -> str:
    if not isinstance(v, float):
        return json.dumps(v)
    s = f"{v:.6f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def replace_rate(text: str, name: str, rate: dict) -> str:
    """Rewrite one model's price object in place, leaving the file's layout alone."""
    body = ", ".join(f'"{k}": {num(v)}' for k, v in rate.items())
    pattern = re.compile(rf'("{re.escape(name)}":\s*)\{{[^}}]*\}}')
    new, n = pattern.subn(lambda m: m.group(1) + "{" + body + "}", text, count=1)
    if n != 1:
        raise SystemExit(f"could not locate the price object for {name} in rates.json")
    return new


def self_check() -> int:
    """The rules that would break silently, pinned against a fixture."""
    if not __debug__:
        raise SystemExit("-O strips every assert here; run without it")
    table = {
        # OpenAI: no charge to write a cache entry, so the field is simply absent.
        "gpt-x": {"input_cost_per_token": 1e-06, "output_cost_per_token": 8e-06,
                  "cache_read_input_token_cost": 1e-07},
        # Anthropic: a longer TTL is priced separately, and old models keep a date.
        "claude-x-20250514": {"input_cost_per_token": 3e-06, "output_cost_per_token": 15e-06,
                              "cache_creation_input_token_cost": 3.75e-06,
                              "cache_creation_input_token_cost_above_1hr": 6e-06,
                              "cache_read_input_token_cost": 3e-07},
        "claude-x-1": {"input_cost_per_token": 99e-06},
        # LiteLLM's placeholder for a price it does not know, and the same model
        # again at a re-seller's prices under a prefixed key.
        "zero-x": {"input_cost_per_token": 2e-06, "output_cost_per_token": 0},
        "half-x": {"input_cost_per_token": 2e-06},   # a quote for one field of five
        # Priced on both, but with nothing said about writing a cache entry.
        "new-x": {"input_cost_per_token": 2e-06, "output_cost_per_token": 8e-06,
                  "cache_read_input_token_cost": 1e-07},
        # Quoted on all five — the only kind of quote that clears the flag.
        "full-x": {"input_cost_per_token": 3e-06, "output_cost_per_token": 15e-06,
                   "cache_creation_input_token_cost": 3.75e-06,
                   "cache_creation_input_token_cost_above_1hr": 6e-06,
                   "cache_read_input_token_cost": 3e-07},
        "anthropic.claude-x": {"input_cost_per_token": 90e-06},
        "bedrock/claude-x-20250514": {"input_cost_per_token": 90e-06},
    }

    # A field LiteLLM does not state is never quoted as zero.
    assert quoted_rate(table["gpt-x"]) == {"input": 1.0, "output": 8.0, "cache_read": 0.1}
    ours = {"input": 1.0, "output": 8.0, "cache_write_5m": 1.25,
            "cache_write_1h": 1.25, "cache_read": 0.1}
    assert {**ours, **quoted_rate(table["gpt-x"])} == ours, "absent field overwrote ours"

    # Nor is a stated zero — or a negative, or something that is not a number.
    assert quoted_rate(table["zero-x"]) == {"input": 2.0}
    assert quoted_rate({"input_cost_per_token": -1e-06}) == {}
    assert quoted_rate({"input_cost_per_token": "3e-06"}) == {}
    assert quoted_rate({"input_cost_per_token": 4e-13}) == {}, "rounded to zero and copied"
    # A zero on the preferred source still falls through to the fallback.
    assert quoted_rate({"cache_creation_input_token_cost_above_1hr": 0,
                        "cache_creation_input_token_cost": 2e-06})["cache_write_1h"] == 2.0

    # A dated variant resolves; a longer-numbered sibling never gets swallowed.
    assert litellm_entry("claude-x", table)[0] == "claude-x-20250514"
    assert litellm_entry("gpt-x", table)[0] == "gpt-x"
    assert litellm_entry("nope", table)[0] is None
    assert quoted_rate(table["claude-x-20250514"])["cache_write_1h"] == 6.0
    # 1h falls back to the 5m write where no one prices the TTL separately.
    assert quoted_rate({"cache_creation_input_token_cost": 2e-06})["cache_write_1h"] == 2.0

    # Float noise from per-token units never reaches the file.
    assert quoted_rate({"cache_read_input_token_cost": 4e-07})["cache_read"] == 0.4
    assert num(0.4) == "0.4" and num(5.0) == "5.0" and num(1.5625) == "1.5625"

    # In-place rewrite touches one object and leaves the layout alone.
    text = '{\n  "a": {"input": 1.0},\n  "ab": {"input": 2.0}\n}\n'
    out = replace_rate(text, "a", {"input": 9.0})
    assert out == '{\n  "a": {"input": 9.0},\n  "ab": {"input": 2.0}\n}\n', out
    assert json.loads(out)["ab"]["input"] == 2.0
    # …but the regex is not scoped, and a duplicate key is valid JSON that loads
    # as the *last* one while the rewrite hits the first. json.loads cannot see
    # that; reading the result back and comparing it, as --apply does, can.
    dup = '{"models": {"a": {"input": 1.0}, "a": {"input": 2.0}}}'
    assert json.loads(replace_rate(dup, "a", {"input": 9.0}))["models"]["a"]["input"] == 2.0

    # End to end on a fixture, for the rules that live in the loop rather than in
    # a function: a `reference` model is never touched, a real quote replaces an
    # estimate and takes the flag with it, and a requested model gets added.
    fixture = ('{\n  "updated": "2000-01-01",\n  "sources": {\n    "openai": "x"\n  },\n\n'
               '  "models": {\n'
               '    "gpt-x":   {"input": 9.0, "output": 8.0, "estimated": true},\n'
               '    "half-x":  {"input": 9.0, "output": 9.0, "estimated": true},\n'
               '    "full-x":  {"input": 9.0, "output": 9.0, "estimated": true},\n'
               '    "local-x": {"input": 9.0, "output": 9.0, "reference": true}\n'
               '  },\n\n  "aliases": {}\n}\n')
    with tempfile.TemporaryDirectory() as d:
        rates, source = Path(d) / "rates.json", Path(d) / "table.json"
        source.write_text(json.dumps(table))
        rates.write_text(fixture)
        base = ["--rates", str(rates), "--source", str(source)]
        argv = base + ["--add", "claude-x", "new-x"]
        seen = io.StringIO()
        with contextlib.redirect_stdout(seen):
            assert main(argv) == 1, "a dry run that found drift exited 0"
            assert main(argv + ["--apply"]) == 0
            assert main(argv) == 0, "not idempotent — a second run still finds drift"
            # An --add that could not happen is not "everything is current".
            assert main(base + ["--add", "zero-x"]) == 1
        assert "→ dropped" in seen.getvalue(), "the dry run hid the estimated flag it drops"
        after = json.loads(rates.read_text())
        assert after["models"]["local-x"] == {"input": 9.0, "output": 9.0, "reference": True}
        # gpt-x is quoted on three fields of five, so the flag it carried stays.
        assert after["models"]["gpt-x"] == {"input": 1.0, "output": 8.0,
                                            "cache_read": 0.1, "estimated": True}
        assert after["models"]["claude-x"]["input"] == 3.0        # the dated build, not 90.0
        # zero-x quotes an input and a placeholder 0 for output. tokentab prices
        # every model on both, so it is reported rather than written half-priced.
        assert "zero-x" not in after["models"]
        assert "no input and output pair" in seen.getvalue()
        # new-x is priced on both but says nothing about cache writes, which will
        # therefore value at nothing — so it is added carrying the flag that says
        # some of these numbers are not quotes.
        assert after["models"]["new-x"] == {"input": 2.0, "output": 8.0,
                                            "cache_read": 0.1, "estimated": True}
        # claude-x and full-x are quoted on all five, so neither needs the warning
        # — and full-x, which carried it, has it taken off.
        assert "estimated" not in after["models"]["claude-x"]
        assert after["models"]["full-x"] == {"input": 3.0, "output": 15.0, "cache_write_5m": 3.75,
                                             "cache_write_1h": 6.0, "cache_read": 0.3}
        # LiteLLM priced one of half-x's two fields, so `output` is still a guess
        # and the flag that says so has to stay.
        assert after["models"]["half-x"] == {"input": 2.0, "output": 9.0, "estimated": True}
        assert after["updated"] != "2000-01-01" and "litellm" in after["sources"]

        # Every price that was overwritten is logged first, so the days before
        # today keep pricing at it. This is what makes the rewrite safe to run
        # unattended: it adds a record, it never edits one.
        today = datetime.now(timezone.utc).date().isoformat()
        hist = json.loads((Path(d) / "price_history.json").read_text())["history"]
        assert set(hist) == {"gpt-x", "half-x", "full-x"}, hist
        assert hist["gpt-x"] == [{"until": today, "input": 9.0, "output": 8.0,
                                  "estimated": True}], hist["gpt-x"]
        # A model that was added has no past to keep, and a `reference` model is
        # never rewritten, so neither of them is logged.
        assert not {"claude-x", "new-x", "local-x"} & set(hist)
        # A second change on the same day must not overwrite the record — it
        # already describes every day before today, and none of those moved.
        assert log_past(Path(d) / "price_history.json", [("gpt-x", {"input": 5.0})], today) == 0
        assert json.loads((Path(d) / "price_history.json").read_text()
                          )["history"]["gpt-x"][0]["input"] == 9.0

        # A model named twice is added once, not twice — a duplicate key is valid
        # JSON whose first copy nothing can ever see again.
        dupes = Path(d) / "dupes.json"
        dupes.write_text(fixture)
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["--rates", str(dupes), "--source", str(source),
                         "--add", "claude-x", "claude-x", "--apply"]) == 0
        assert dupes.read_text().count('"claude-x":') == 1

        # An --add whose anchor is missing rewrites nothing. It must say so.
        blind = Path(d) / "blind.json"
        blind.write_text('{\n  "sources": {},\n  "models": {\n    "gpt-x": {"input": 1.0}\n  }\n}\n')
        before = blind.read_text()
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                main(["--rates", str(blind), "--source", str(source), "--add", "claude-x", "--apply"])
            except SystemExit as e:
                assert "did not take for claude-x" in str(e), e
            else:
                raise AssertionError("an --add that rewrote nothing reported success")
        assert blind.read_text() == before, "left a half-written file behind"

        # The date and the source line are edits the file has to have a place
        # for. Missing the place is not a failure — refusing to write the prices
        # over an absent provenance note would be.
        bare = Path(d) / "bare.json"
        bare.write_text('{"models": {"gpt-x": {"input": 1.0}}}')
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["--rates", str(bare), "--source", str(source), "--apply"]) == 0
        got = json.loads(bare.read_text())
        assert got["models"]["gpt-x"]["output"] == 8.0 and "updated" not in got

        # An existing sources.litellm is left as the file states it, pin and all.
        pinned = fixture.replace('"openai": "x"', '"litellm": "http://pinned/v1"')
        pin = Path(d) / "pin.json"
        pin.write_text(pinned)
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["--rates", str(pin), "--source", str(source), "--apply"]) == 0
        assert json.loads(pin.read_text())["sources"] == {"litellm": "http://pinned/v1"}

        # And what --add writes has to be priceable. A model whose vendor does not
        # charge to write a cache entry gets no cache-write fields at all, and
        # tokentab reads a rate object straight out of this file.
        tt = tokentab_module()
        assert tt, "tokentab.py sits next to this tool; if it will not load, say so"
        row = {"model": "half-x", "provider": "openai", "input": 1, "output": 1,
               "cache_read": 1, "cache_write": 1, "cache_write_1h": 1}
        assert tt.value_of(row, json.loads(rates.read_text())) > 0

        # And the log is what tokentab prices past days from: before the change
        # the old rate, on and after it the new one. A row with no day at all —
        # an aggregate spanning several — prices at the current rate.
        #
        # Loaded, not hand-assembled. Building `_history` here instead is what
        # let the two halves drift apart once already: `load_rates` is the only
        # thing that decides which fields a record may carry, and it refuses the
        # ones it does not know — so a log written with a field it rejects stops
        # `report`, `verify`, `ingest` and `reprice` alike, every way out of it
        # and the way back in. This is the round trip that would have said so.
        tt.CONFIG_DIR = Path(d)
        dated = tt.load_rates()
        assert set(dated["_history"]) == set(hist), dated["_history"]
        old_day = {"model": "gpt-x", "provider": "openai", "day": "1999-01-01",
                   "input": 1_000_000, "output": 0, "cache_read": 0,
                   "cache_write": 0, "cache_write_1h": 0}
        assert tt.value_of(old_day, dated) == 9.0
        assert tt.value_of({**old_day, "day": today}, dated) == 1.0
        assert tt.value_of({k: v for k, v in old_day.items() if k != "day"}, dated) == 1.0

        # Several records for one model, written out of order — the file is
        # hand-editable and nothing about it is sorted. load_rates is what puts
        # them in order, and without that the lookup returns the first record it
        # happens to meet, which understates a past day without failing anything.
        tt.CONFIG_DIR = Path(d)
        (Path(d) / "price_history.json").write_text(json.dumps({"history": {"gpt-x": [
            {"until": "2026-06-01", "input": 30.0, "output": 0.0},
            {"until": "2026-02-01", "input": 10.0, "output": 0.0},
            {"until": "2026-04-01", "input": 20.0, "output": 0.0},
        ]}}))
        loaded = tt.load_rates()
        priced = [tt.value_of({**old_day, "day": day}, loaded) for day in
                  ("2026-01-15", "2026-02-01", "2026-03-31", "2026-04-01", "2026-06-01", None)]
        assert priced == [10.0, 20.0, 20.0, 30.0, 1.0, 1.0], priced

        # And a log that would otherwise surface as a traceback out of `report`
        # says which file and what is wrong with it instead.
        for broken in ({"gpt-x": {}}, {"gpt-x": [{"input": 1.0, "output": 1.0}]},
                       {"gpt-x": [{"until": "2026-06-01T00:00:00Z", "input": 1.0, "output": 1.0}]},
                       {"gpt-x": [{"until": "2026-06-01", "output": 1.0}]}):
            (Path(d) / "price_history.json").write_text(json.dumps({"history": broken}))
            try:
                tt.load_rates()
            except SystemExit as e:
                assert "price_history.json" in str(e), e
            else:
                raise AssertionError(f"loaded a log it cannot price from: {broken}")

        # price_rows walks the store a chunk at a time, in rowid order. Walking it
        # wrong loses rows quietly: a report is simply short, and nothing fails.
        # So: more rows than one chunk, and the count has to be every one of
        # them, twice — once unpriced, once as a full reprice. Two of them carry
        # no id, because walking on `id` steps straight over those (`NULL > ''`
        # is NULL, `'' > ''` is false) and leaves a store no report will add up
        # and no command can fix.
        (Path(d) / "price_history.json").write_text(json.dumps({"history": {}}))
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(tt.SCHEMA)
        con.executemany(
            "INSERT INTO events (id,ts,day,host,source,provider,billing,account,model,"
            "project,repo,input,output,cache_read,cache_write,cache_write_1h,cash_usd,"
            f"value_usd) VALUES ({','.join('?' * 18)})",
            [(f"e{i:05d}", f"{today}T00:00:00Z", today, "h", "s", "openai", "metered", "",
              "gpt-x", "p", "r", 1_000_000, 0, 0, 0, 0, 0.0, None) for i in range(250)])
        con.executemany(
            "INSERT INTO events (id,ts,day,host,source,provider,billing,account,model,"
            "project,repo,input,output,cache_read,cache_write,cache_write_1h,cash_usd,"
            f"value_usd) VALUES ({','.join('?' * 18)})",
            [(nid, f"{today}T00:00:00Z", today, "h", "s", "openai", "metered", "",
              "gpt-x", "p", "r", 1_000_000, 0, 0, 0, 0, 0.0, None) for nid in (None, "")])
        con.commit()
        loaded = tt.load_rates()

        # A row carrying no Value adds nothing to a SUM, so a store migrated but
        # never priced would report $0.00 rather than fail. That silence is the
        # thing this whole change exists to remove: every report of a window
        # holding one has to refuse.
        window = {"preset": "all", "from": None, "to": None,
                  "_from": date(2000, 1, 1), "_to": date(2999, 1, 1)}
        blank = {k: None for k in tt.FILTER_COLS}
        try:
            tt.summarise(con, loaded, {}, window, blank)
        except SystemExit as e:
            assert "reprice" in str(e), e
        else:
            raise AssertionError("added up a window holding rows that carry no Value")

        assert tt.price_rows(con, loaded, chunk=100) == 252
        assert con.execute("SELECT COUNT(*) FROM events WHERE value_usd IS NULL").fetchone()[0] == 0
        assert tt.price_rows(con, loaded, chunk=100, apply=False) == 0
        assert tt.summarise(con, loaded, {}, window, blank)["headline"]["value_usd"] > 0
        loaded["models"]["gpt-x"]["input"] *= 2
        assert tt.price_rows(con, loaded, chunk=100, apply=False) == 252
        # …and a dry run leaves every one of them exactly as it found them.
        assert tt.price_rows(con, loaded, chunk=100, apply=False) == 252

    print("  self-check: all rules hold")
    return 0


def iso_date(v: str) -> bool:
    """A real calendar date. 2026-99-99 has the shape and is not one, and every
    comparison against it is a string comparison that would sort past December."""
    try:
        return bool(date.fromisoformat(v))
    except ValueError:
        return False


# What a logged record may carry: the price fields, and the two provenance flags
# `load_rates` allows beside them. Anything else in a rates.json entry is a hand
# annotation, and copying it into the log makes the log unreadable.
LOGGABLE = {*FIELDS, "estimated", "reference"}


def log_past(path: Path, outgoing: list, until: str) -> int:
    """Record what these models cost until `until`, so past days keep pricing at it.

    Overwriting a price in rates.json is the one thing here that reaches
    backwards: an event already stored keeps the Value it was priced at, but a
    backfill, a re-scan or a `reprice` prices a past day from this file, so
    without the log a rate cut yesterday re-prices every day it reaches.

    Append-only, and always at the end: each record's implied start is the
    previous record's `until`, so a date earlier than one already logged does
    not add a period, it silently re-dates every period above it — the days
    that were priced at the older rate get the newer one and vice versa. A
    model that already has a record ending on this day keeps the one it has,
    for the same reason: that record prices every day *before* today, and a
    second change today does not alter a single one of them.
    """
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        doc = {}
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"could not read {path} ({e}) — rates.json left unchanged")
    hist = doc.setdefault("history", {})
    # Hand-edited, by invitation: the file's own `_format` note tells the reader
    # what a record looks like. Everything below sorts and compares on `until`,
    # so a record without one is a traceback out of a tool documented as safe to
    # run unattended — and this runs before the write, so nothing is half-logged.
    if not isinstance(hist, dict):
        raise SystemExit(f"{path}: `history` is not an object of model → prices. "
                         f"rates.json left unchanged.")
    for name, recs in hist.items():
        if not isinstance(recs, list) or not all(
                isinstance(r, dict) and isinstance(r.get("until"), str) for r in recs):
            raise SystemExit(f"{path}: {name} is not a list of prices each carrying an "
                             f"`until` date. Fix it by hand; rates.json left unchanged.")
    written = 0
    for name, was in outgoing:
        recs = hist.setdefault(name, [])
        newest = max((str(r.get("until", "")) for r in recs), default="")
        if newest == until:
            continue
        if newest > until:
            raise SystemExit(f"{name}: price_history.json already ends a price on {newest}, "
                             f"which is after {until}. The outgoing price is always the "
                             f"newest one — logging an earlier date would re-date every "
                             f"period after it. rates.json left unchanged.")
        # Filtered, not copied whole: `load_rates` refuses a record carrying a
        # field it does not know, and refusing happens on every read — so one
        # stray key in rates.json (a note somebody added by hand) would be
        # logged here and then stop `report`, `verify` and `reprice` alike.
        recs.append({"until": until,
                     **{k: v for k, v in was.items() if k in LOGGABLE}})
        recs.sort(key=lambda r: r["until"])
        written += 1
    if not written:
        return 0
    try:
        tmp = path.with_name(path.name + ".tmp")   # an interrupt must not truncate it
        tmp.write_text(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        raise SystemExit(f"could not write {path}: {e}")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-check", action="store_true", help="run the built-in assertions and exit")
    ap.add_argument("--apply", action="store_true",
                    help="write rates.json (default is a dry run, which exits 1 on drift)")
    ap.add_argument("--add", nargs="+", metavar="MODEL", default=[],
                    help="also start pricing these models, by their LiteLLM name")
    ap.add_argument("--source", default=SOURCE,
                    help="LiteLLM price table — a URL, or a path to a saved copy")
    ap.add_argument("--rates", default=str(RATES))
    ap.add_argument("--as-of", metavar="YYYY-MM-DD",
                    help="the day the new prices took effect. Defaults to today, which is the "
                         "day this ran — every day between the vendor's change and this run is "
                         "then priced at the old rate. Pass the real date when you know it.")
    args = ap.parse_args(argv)
    if args.self_check:
        return self_check()

    # Checked here, not where it is used: down there it is only reached when this
    # run happens to find drift, so a scheduled job with a typo in the date would
    # pass every quiet week and fail on the one day it had a price to log.
    if args.as_of:
        today = datetime.now(timezone.utc).date().isoformat()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.as_of) or not iso_date(args.as_of):
            raise SystemExit(f"--as-of wants a YYYY-MM-DD date, not {args.as_of!r}")
        # A price cannot stop applying tomorrow: `until` is the day the new rate
        # takes over, so a future one prices days that have not happened at a rate
        # nobody has charged, and `tokentab verify` fails on it afterwards.
        if args.as_of > today:
            raise SystemExit(f"--as-of {args.as_of} is in the future — a price can only have "
                             f"stopped applying on a day that has happened.")

    src = Path(args.source)
    try:
        if src.is_file():
            table = json.loads(src.read_text())
        else:
            if "://" not in args.source:
                what = "not a file" if src.exists() else "no such file"
                raise SystemExit(f"{what}: {args.source} (and it is not a URL)")
            with urllib.request.urlopen(args.source, timeout=30) as r:
                table = json.loads(r.read())
    except json.JSONDecodeError as e:
        raise SystemExit(f"{args.source} is not the JSON price table: {e}")
    except (OSError, UnicodeDecodeError) as e:   # a directory, a timeout, a dead
        raise SystemExit(f"could not read {args.source}: {e}")   # host, binary junk

    path = Path(args.rates).expanduser()
    try:
        text = path.read_text()
        own = json.loads(text)
    except (OSError, UnicodeDecodeError) as e:
        raise SystemExit(f"could not read {path}: {e}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path} is not valid JSON: {e}")
    if not isinstance(own.get("models"), dict):
        raise SystemExit(f'{path} has no "models" block — not a tokentab rates file')

    print(f"\n  tokentab rates · {len(table):,} models quoted upstream")
    print(f"  {args.source}\n")

    priced_from = in_use()
    if priced_from and priced_from.exists() and priced_from.resolve() != path.resolve():
        print(f"  editing {path}")
        print(f"  note: the installed tokentab prices from {priced_from} — "
              f"re-run install.sh to carry a change across.\n")

    requested = [m for m in dict.fromkeys(args.add) if m not in own["models"]]
    changes: list[tuple[str, dict]] = []
    unquoted, not_found, unpriced, unstated = [], [], [], 0
    for name in list(own["models"]) + requested:
        current = own["models"].get(name, {})
        if current.get("reference"):
            continue
        key, entry = litellm_entry(name, table)
        if entry is None:
            (not_found if name in requested else unquoted).append(name)
            continue
        quoted = quoted_rate(entry)
        merged = {**current, **quoted}
        if not {"input", "output"} <= merged.keys():
            # tokentab reads both without asking, so a model without both is not
            # one this can price. Either the upstream entry has no pair to start
            # from — an embedding model, or one carrying placeholder zeros — or
            # the row already in the file is truncated. Both get reported and
            # left alone rather than written half-priced.
            unpriced.append(name)
            continue
        unstated += len(FIELDS) - len(quoted)
        # `estimated` says some of these numbers have no source behind them. A
        # field LiteLLM leaves unstated prices at zero, and nothing here can tell
        # "the vendor does not charge" from "upstream has no number" — so a model
        # added on a partial quote is flagged, or its cache writes would value at
        # nothing without a word. An existing row is not: its gaps were filled by
        # hand from the vendor's own page, which rates.json cites as a source and
        # LiteLLM does not supersede. Only a quote covering all five clears it,
        # since only that leaves nothing unsourced.
        full = quoted.keys() == FIELDS.keys()
        estimated = merged.pop("estimated") if ("estimated" in merged and full) else None
        if name in requested and not full:
            merged["estimated"] = True
        if merged != current:
            changes.append((name, merged))
            for field, value in quoted.items():
                if field in current and abs(current[field] - value) > 1e-9:
                    print(f"    {name:<20} {field:<15} {num(current[field]):>10} → {num(value)}")
                elif field not in current:
                    print(f"    {name:<20} {field:<15} {'—':>10} → {num(value)}")
            # a real quote replaces the stand-in, so the flag goes with it — and
            # on its own that is the whole change, which a dry run must still show
            if estimated is not None:
                print(f"    {name:<20} {'estimated':<15} {json.dumps(estimated):>10} → dropped")

    if unquoted:
        print(f"\n  no first-party quote upstream — left as hand-maintained:")
        for name in unquoted:
            print(f"    {name}")
    if not_found:
        print(f"\n  asked for, but LiteLLM does not list them — not added:")
        for name in not_found:
            print(f"    {name}")
    if unpriced:
        print(f"\n  no input and output pair to price on — nothing added or rewritten:")
        for name in unpriced:
            print(f"    {name}")
    if unstated:
        print(f"\n  {unstated} fields left as they are — LiteLLM quotes no price for them "
              f"(a vendor that does not charge for a cache write has no number to copy, "
              f"and a price upstream does not know is written as a 0, which is not one either).")

    missed = bool(not_found or unpriced)   # an --add that did not happen is not "current"
    if not changes:
        print("\n  rates.json agrees with LiteLLM. Nothing to do.\n")
        return 1 if missed else 0

    if not args.apply:
        print(f"\n  {len(changes)} model(s) would change. Dry run — re-run with --apply.\n")
        return 1  # so a scheduled check can fail on drift without a wrapper

    for name, rate in changes:
        if name in own["models"]:
            text = replace_rate(text, name, rate)
        else:
            body = ", ".join(f'"{k}": {num(v)}' for k, v in rate.items())
            text = text.replace('\n  },\n\n  "aliases"',
                                f',\n    "{name}": {{{body}}}\n  }},\n\n  "aliases"', 1)
    today = datetime.now(timezone.utc).date().isoformat()
    text, stamped = re.subn(r'("updated":\s*)"[^"]*"', rf'\1"{today}"', text, count=1)
    # Only what the file already has a place for. A rates.json with no `updated`
    # key or no `sources` block is owed neither line, and refusing to write over
    # a missing provenance note would throw the prices away with it.
    at = None if "litellm" in own.get("sources", {}) else re.search(r'"sources"\s*:\s*\{', text)
    if at:
        comma = "," if own.get("sources") else ""  # an empty sources block takes none
        text = text[:at.end()] + f'\n    "litellm": "{SOURCE}"{comma}' + text[at.end():]

    # Never write a file tokentab cannot load — and never report a change the
    # rewrite did not actually make. Both edits above are textual: a missing
    # anchor is a silent no-op, and a duplicate key would rewrite the wrong one
    # while json.loads kept the other. Reading the result back catches both.
    try:
        written = json.loads(text)
    except json.JSONDecodeError as e:
        raise SystemExit(f"the rewrite would not have loaded ({e}) — {path} left unchanged")
    for name, rate in changes:
        if written["models"].get(name) != rate:
            raise SystemExit(f"rewrite did not take for {name} — {path} left unchanged")
    if stamped and written.get("updated") != today:
        raise SystemExit(f"the updated date did not take — {path} left unchanged")
    if at and written.get("sources", {}).get("litellm") != SOURCE:
        raise SystemExit(f"the sources.litellm line did not take — {path} left unchanged")
    # The outgoing prices, logged before the new ones land — and logged first, so
    # an interrupt between the two leaves a record saying the days before today
    # cost what rates.json still says they cost, which prices them exactly as
    # before. Only a model that had a price: a newly added one has no past to
    # keep, and neither does a change that moved no number (a dropped flag).
    moved = [(n, own["models"][n]) for n, rate in changes if n in own["models"]
             and any(abs(own["models"][n].get(f, -1) - rate.get(f, -1)) > 1e-9 for f in FIELDS)]
    effective = args.as_of or today  # validated in main, before anything is fetched
    logged = log_past(path.with_name("price_history.json"), moved, effective)

    try:
        tmp = path.with_name(path.name + ".tmp")  # an interrupt must not truncate it
        tmp.write_text(text)
        os.replace(tmp, path)
    except OSError as e:
        raise SystemExit(f"could not write {path}: {e}")
    print(f"\n  wrote {path} — {len(changes)} model(s), updated {today}.")
    if logged:
        print(f"  {logged} outgoing price(s) logged to price_history.json — days before "
              f"{effective} keep pricing at them."
              + ("" if args.as_of else "  (--as-of sets that date; it is today by default, "
                                       "which is when this ran, not when the vendor moved.)"))
    print()
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
