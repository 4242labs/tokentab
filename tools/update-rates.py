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
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RATES = HERE.parent / "rates.json"
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
               '    "local-x": {"input": 9.0, "output": 9.0, "reference": true}\n'
               '  },\n\n  "aliases": {}\n}\n')
    with tempfile.TemporaryDirectory() as d:
        rates, source = Path(d) / "rates.json", Path(d) / "table.json"
        source.write_text(json.dumps(table))
        rates.write_text(fixture)
        argv = ["--rates", str(rates), "--source", str(source), "--add", "claude-x", "zero-x"]
        seen = io.StringIO()
        with contextlib.redirect_stdout(seen):
            assert main(argv) == 1, "a dry run that found drift exited 0"
            assert main(argv + ["--apply"]) == 0
            assert main(argv) == 0, "not idempotent — a second run still finds drift"
        assert "estimated" in seen.getvalue(), "the dry run hid the estimated flag it drops"
        after = json.loads(rates.read_text())
        assert after["models"]["local-x"] == {"input": 9.0, "output": 9.0, "reference": True}
        assert after["models"]["gpt-x"] == {"input": 1.0, "output": 8.0, "cache_read": 0.1}
        assert after["models"]["claude-x"]["input"] == 3.0        # the dated build, not 90.0
        assert after["models"]["zero-x"] == {"input": 2.0}        # the 0 was not copied
        assert after["updated"] != "2000-01-01" and "litellm" in after["sources"]

        # An --add whose anchor is missing rewrites nothing. It must say so.
        blind = Path(d) / "blind.json"
        blind.write_text('{\n  "sources": {},\n  "models": {\n    "gpt-x": {"input": 1.0}\n  }\n}\n')
        before = blind.read_text()
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                main(["--rates", str(blind), "--source", str(source), "--add", "claude-x", "--apply"])
            except SystemExit as e:
                assert "did not take" in str(e), e
            else:
                raise AssertionError("an --add that rewrote nothing reported success")
        assert blind.read_text() == before, "left a half-written file behind"

    print("  self-check: all rules hold")
    return 0


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
    args = ap.parse_args(argv)
    if args.self_check:
        return self_check()

    src = Path(args.source)
    try:
        if src.exists():
            table = json.loads(src.read_text())
        else:
            if "://" not in args.source:
                raise SystemExit(f"no such file: {args.source} (and it is not a URL)")
            with urllib.request.urlopen(args.source, timeout=30) as r:
                table = json.loads(r.read())
    except json.JSONDecodeError as e:
        raise SystemExit(f"{args.source} is not the JSON price table: {e}")
    except urllib.error.URLError as e:
        raise SystemExit(f"could not fetch {args.source}: {e}")
    path = Path(args.rates).expanduser()
    text = path.read_text()
    own = json.loads(text)

    print(f"\n  tokentab rates · {len(table):,} models quoted upstream")
    print(f"  {args.source}\n")

    requested = [m for m in args.add if m not in own["models"]]
    changes: list[tuple[str, dict]] = []
    unquoted, not_found, unstated = [], [], 0
    for name in list(own["models"]) + requested:
        current = own["models"].get(name, {})
        if current.get("reference"):
            continue
        key, entry = litellm_entry(name, table)
        if entry is None:
            (not_found if name in requested else unquoted).append(name)
            continue
        quoted = quoted_rate(entry)
        unstated += len(FIELDS) - len(quoted)
        merged = {**current, **quoted}
        estimated = merged.pop("estimated", None)
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
    if unstated:
        print(f"\n  {unstated} fields left as they are — LiteLLM quotes no price for them "
              f"(a vendor that does not charge for a cache write has no number to copy, "
              f"and a price upstream does not know is written as a 0, which is not one either).")

    if not changes:
        print("\n  rates.json agrees with LiteLLM. Nothing to do.\n")
        return 0

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
    text = re.sub(r'("updated":\s*)"[^"]*"', rf'\1"{today}"', text, count=1)
    if "litellm" not in own.get("sources", {}):
        comma = "," if own.get("sources") else ""  # an empty sources block takes none
        text = text.replace('"sources": {', f'"sources": {{\n    "litellm": "{SOURCE}"{comma}', 1)

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
    tmp = path.with_name(path.name + ".tmp")  # an interrupt must not truncate rates.json
    tmp.write_text(text)
    os.replace(tmp, path)
    print(f"\n  wrote {path} — {len(changes)} model(s), updated {today}.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
