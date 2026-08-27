#!/usr/bin/env python3
"""Check rates.json against LiteLLM's published price table, and update it.

The Value number is the whole reason tokentab exists — usage priced at list
rates — and it is only as good as rates.json. Hand-typed, that file goes stale
silently: a vendor cuts a price, nothing errors, and every historical Value is
quietly wrong. LiteLLM maintains the price table this reads instead.

    python3 tools/update-rates.py            # dry run — report drift
    python3 tools/update-rates.py --apply    # write rates.json
    python3 tools/update-rates.py --add gpt-5.7      # start pricing a new model

Two rules keep this from doing damage:

  * **A field LiteLLM does not state is not a quote of zero.** OpenAI does not
    charge to write a cache entry, so LiteLLM simply omits the field. Reading
    that as $0 would silently rewrite nine models' cache pricing. Only fields
    LiteLLM actually states are ever copied.
  * **Local models are never touched.** Their rates are marked `reference` —
    deliberate stand-ins for a comparable hosted model, not quotes — and no
    upstream can have an opinion about them.

Everything else in rates.json (aliases, free models, the fallbacks, comments,
and the layout) is left exactly as it was: this rewrites the price objects of
the models it can quote, in place, and nothing else.

Stdlib only, like the rest of tokentab.
"""

import argparse
import json
import re
import sys
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

    Only bare, first-party keys are considered. LiteLLM also carries the same
    models under `anthropic.…-v1:0` and `bedrock/…` names at *Bedrock's*
    prices, and quietly pricing a Claude subscription off a Bedrock quote is
    exactly the kind of wrong number this file exists to prevent. A model with
    no first-party quote is reported as hand-maintained instead.
    """
    if name in table:
        return name, table[name]
    dated = re.compile(rf"^{re.escape(name)}-\d{{8}}$")
    for key in sorted(table, reverse=True):  # newest dated build first
        if dated.match(key):
            return key, table[key]
    return None, None


def quoted_rate(entry: dict) -> dict:
    """The fields LiteLLM states, in tokentab's units. Absent stays absent."""
    out = {}
    for field, sources in FIELDS.items():
        for src in sources:
            if entry.get(src) is not None:
                out[field] = round(entry[src] * 1_000_000, 6)
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
    """The two rules that would break silently, pinned against a fixture."""
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
    }

    # A field LiteLLM does not state is never quoted as zero.
    assert quoted_rate(table["gpt-x"]) == {"input": 1.0, "output": 8.0, "cache_read": 0.1}
    ours = {"input": 1.0, "output": 8.0, "cache_write_5m": 1.25,
            "cache_write_1h": 1.25, "cache_read": 0.1}
    assert {**ours, **quoted_rate(table["gpt-x"])} == ours, "absent field overwrote ours"

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

    print("  self-check: all rules hold")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-check", action="store_true", help="run the built-in assertions and exit")
    ap.add_argument("--apply", action="store_true", help="write rates.json (default is a dry run)")
    ap.add_argument("--add", nargs="+", metavar="MODEL", default=[],
                    help="also start pricing these models, by their LiteLLM name")
    ap.add_argument("--source", default=SOURCE,
                    help="LiteLLM price table — a URL, or a path to a saved copy")
    ap.add_argument("--rates", default=str(RATES))
    args = ap.parse_args()
    if args.self_check:
        return self_check()

    src = Path(args.source)
    if src.exists():
        table = json.loads(src.read_text())
    else:
        with urllib.request.urlopen(args.source, timeout=30) as r:
            table = json.loads(r.read())
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
        merged.pop("estimated", None)
        if merged != current:
            changes.append((name, merged))
            for field, value in quoted.items():
                if field in current and abs(current[field] - value) > 1e-9:
                    print(f"    {name:<20} {field:<15} {num(current[field]):>10} → {num(value)}")
                elif field not in current:
                    print(f"    {name:<20} {field:<15} {'—':>10} → {num(value)}")

    if unquoted:
        print(f"\n  no first-party quote upstream — left as hand-maintained:")
        for name in unquoted:
            print(f"    {name}")
    if not_found:
        print(f"\n  asked for, but LiteLLM does not list them — not added:")
        for name in not_found:
            print(f"    {name}")
    if unstated:
        print(f"\n  {unstated} fields left as they are — LiteLLM states no price for them "
              f"(a vendor that does not charge for a cache write has no number to copy).")

    if not changes:
        print("\n  rates.json agrees with LiteLLM. Nothing to do.\n")
        return 0

    if not args.apply:
        print(f"\n  {len(changes)} model(s) would change. Dry run — re-run with --apply.\n")
        return 0

    for name, rate in changes:
        if name in own["models"]:
            text = replace_rate(text, name, rate)
        else:
            body = ", ".join(f'"{k}": {num(v)}' for k, v in rate.items())
            text = text.replace('\n  },\n\n  "aliases"',
                                f',\n    "{name}": {{{body}}}\n  }},\n\n  "aliases"', 1)
    today = datetime.now(timezone.utc).date().isoformat()
    text = re.sub(r'("updated":\s*)"[^"]*"', rf'\1"{today}"', text, count=1)
    if '"litellm"' not in text:
        text = text.replace('"sources": {', f'"sources": {{\n    "litellm": "{SOURCE}",', 1)

    json.loads(text)  # never write a file tokentab cannot load
    path.write_text(text)
    print(f"\n  wrote {path} — {len(changes)} model(s), updated {today}.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
