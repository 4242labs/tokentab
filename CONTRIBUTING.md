# Contributing

**Status: passively maintained.** tokentab runs in production at 42labs and gets
commits regularly — but it is not a staffed product. There is no support rota and
no SLA. Issues and pull requests are welcome and genuinely read; expect a reply in
weeks rather than days, and sometimes not at all. That is capacity, not
disinterest. Plan accordingly before you invest a weekend.

## What's welcome

- **Bug reports with a reproduction.** For a pricing or allocation bug, the useful
  repro is a `plans.json` (with your accounts replaced by placeholders), the
  period, and the figure you expected — not a screenshot.
- **Small, focused pull requests.** One logical change.
- **A rate correction.** `rates.json` records its sources; if a published price
  changed, a PR that updates the number *and* its source line is the easiest kind
  to merge. Value is written onto each event at the rate in force the day the usage
  happened, so a corrected rate applies to new events only. Re-pricing history is
  something you run on purpose, with `reprice --apply`.
- **A new source.** The collector reads transcripts other tools already write.
  Another CLI that leaves a parseable log on disk is a natural fit.
- **Documentation** — typos, unclear passages, missing setup steps.

## What is unlikely to land

- Anything that requires a vendor API key, a proxy in front of your agent, or
  stored credentials. That is the one architectural line this project holds.
- Presenting an allocated figure as if it were cash. A flat plan has no per-call
  price; a per-project number for it is accounting, and the UI has to keep saying
  so.
- Large refactors, architecture changes, rewrites.
- Features not discussed in an issue first. **Open an issue before you write the
  code** — one message, potentially a saved weekend.
- Unrequested dependency bumps, formatting-only diffs, build-tooling swaps.

## If you need it faster

Fork it. The AGPL-3.0 grants you exactly that. A fork that moves faster than this
repo is a good outcome, not a betrayal — it is the real answer, not a brush-off.

## Before you open a PR

`tokentab.py` is one file, Python 3.10+, **stdlib only** — no pip install, no
virtualenv. Keep it that way.

```sh
python3 -m py_compile tokentab.py    # it has to at least compile
./tokentab.py verify                 # acceptance checks: allocation conservation
                                     # over a whole cycle + hand-priced rate spot-check
cd web && npm ci && npm run build    # only if you touched the dashboard
```

Two things this repo is strict about, because both fail quietly:

- **`plans.json` is data, never code.** It names the accounts you are signed in as.
  It is gitignored; `plans.example.json` carries placeholders. A committed
  `plans.json` publishes your logins.
- **The store is the durable record.** Vendors prune the transcripts this reads
  from, so anything not collected before a prune is gone for good. A change that
  can drop, recreate or silently fork the database needs to be obviously correct —
  `install.sh` deliberately refuses to migrate a store for exactly this reason.

## Licensing

tokentab is dual-licensed: AGPL-3.0 for open source, commercial terms on request —
see [LICENSING.md](LICENSING.md).

**By submitting a pull request you grant 42labs the right to distribute your
contribution under both the AGPL-3.0 and 42labs' commercial license.** You keep
the copyright in what you wrote. Without that grant a single merged patch would
make the commercial half unsellable, and we would have to refuse it.
