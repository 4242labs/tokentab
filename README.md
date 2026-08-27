<img src=".github/logomark.svg" alt="42labs" width="56" />

# tokentab

[![Project Status: Active](https://www.repostatus.org/badges/latest/active.svg)](https://www.repostatus.org/#active)
[![Maintenance](https://img.shields.io/badge/maintenance-passively--maintained-yellowgreen.svg)](CONTRIBUTING.md)

![TokenTab — cash out, allocated cost and value over the last 30 days, with daily value per day](docs/dashboard.png)

**[Live demo →](https://tokentab.42labs.io/)** — 100% synthetic data, no store behind it.

> *What AI actually costs — across providers, projects and machines, with the
> flat-rate plans included rather than excluded.*

Every usage dashboard you can buy meters API keys. Almost nobody pays for coding
agents that way any more: the spend is a handful of $20–$200 subscriptions, and
a subscription has no per-call price to meter. tokentab reads the transcripts
the CLIs already write to disk, prices the usage at published list rates, and
splits each flat fee across the projects that consumed it.

No vendor API key. No proxy in front of your agent. No credentials stored.

---

## The three numbers

| Number | Meaning |
|:--|:--|
| **Cash out** | Real money in the period: flat plan fees + metered API charges |
| **Allocated cost** | A plan's fee split across projects by token share — *accounting, not measurement* |
| **Value** | The same usage priced at published API list rates, for subscription and local traffic alike |

A flat plan has no per-call price, so any per-project figure for it is
**allocated**. The dashboard never presents an allocated number as cash: apply a
project (or repo/model/machine/source) filter and the *Cash out* card switches to
the allocated figure, turns amber, goes dashed and carries an `ALLOCATED` badge.

![One project selected: the Cash out card has switched to the allocated share, amber and dashed, with an ALLOCATED badge and a note explaining why](docs/allocated.png)

**Allocation basis:** total tokens (input + output + cache read + cache write) per
project, per billing cycle. **Billing periods are plan cycles** (`cycle_day` →
same day next month), not calendar months, and fees are never pro-rated.

## Accounts

One vendor can bill you several times over — a plan per login. A **subscription**
is therefore a `plan family × account` pair, and those pairs are **discovered from
the data**, not enumerated in config: sign into a new account and its subscription
appears by itself; cancel one and it stops charging. `plans.json` → `plans` holds
the family template, `accounts` holds optional per-account overrides (`label`,
`monthly_usd`, `cycle_day`, `active_from`, `active_to`). An account with no entry
bills at its family's default fee and says so in the UI.

Transcripts carry no account identity, so the account is read from each CLI's own
config at scan time — `~/.claude.json` → `oauthAccount.emailAddress`, and the
`email` claim of the Codex `id_token`. **The token itself is never read further,
stored or logged.** A host that switches accounts stamps only what it scans
afterwards; history keeps the account it was scanned under.

Filtering by account keeps *Cash out* a cash figure — an account's own plan fee is
real money for that account. Only project/repo/model/machine/source narrowing
switches the card to allocated.

**Cancelled on an unknown date:** set `"active_to": "auto"` and the end is inferred
from that account's last usage, displayed with a trailing `?` so an inference is
never mistaken for an invoice.

Events that predate account tracking are stamped by `tokentab adopt`: each
`host × source` with exactly one known account adopts its orphans, anything
ambiguous is reported and left alone. Without it those rows bill as a phantom
account-less subscription — `tokentab verify` fails if any remain.

## Why not a gateway

Claude Code (Max) and Codex CLI (ChatGPT plan) authenticate by OAuth straight to
their vendor. Repointing them at LiteLLM or any other proxy breaks that auth, and
passing subscription OAuth through third-party infrastructure is a ToS grey zone.
Usage is therefore read from the **local transcripts each CLI already writes**, and
the cost side is the flat fee. No vendor API key, no network interception, nothing
to keep credentials for.

## Sources

| Source | Path | Notes |
|:--|:--|:--|
| `claude_code` | `~/.claude/projects/<encoded-cwd>/<session>.jsonl` | full `usage` blocks incl. cache + thinking tokens |
| `codex` | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | `token_count` events; `last_token_usage` is the per-turn delta |
| `llama_swap` | `~/.config/llama-swap/swap.log` | llama.cpp timing lines; see limits below |

A subscription CLI pointed at a local endpoint is reclassified
`provider=local, billing=local` — that traffic is free and must not consume the
plan's allocation. The *model name*, not the source, decides billing.

## Attribution

By path position, from the session's working directory — no git or filesystem
lookup, so history still resolves after a directory is deleted:

```
<root>/<project>/<repo>/…                     -> (project, repo)
<root>/<project>                              -> (project, project)
<root>/<project>/worktrees/<repo>--<branch>/… -> (project, repo)
anything outside the configured roots         -> ('ad-hoc', <basename>)
```

Roots live in `plans.json` → `roots.paths`.

## Topology

One host collects; the others push to it over SSH.

```
laptop  ─ launchd  com.42labs.tokentab-collect (hourly) ─┐
worksta ─ systemd --user tokentab-collect.timer  ────────┼─► ssh ─► server  tokentab ingest
server  ─ systemd --user tokentab-collect.timer  ────────┘                  └─► SQLite
                                                            tokentab-serve ─► :8899
```

Each host scans only its own transcripts and pushes NDJSON over SSH. Events carry
a content hash as primary key, so re-pushing is idempotent and a full re-backfill
is always safe.

## Install

```sh
git clone <this repo> tokentab && cd tokentab
cp plans.example.json plans.json      # then edit: your plans, accounts, roots
```

Build the dashboard bundle once, on any machine with Node — the serving host needs
only Python:

```sh
cd web && npm ci && npm run build     # -> web/dist/
```

Then run the installer on each host, from a checkout of this repo:

```sh
./install.sh --serve --bind <tailnet-addr> --collect   # the server
./install.sh --collect --server <server-host>          # every other host
tokentab push --server <server-host> --all             # one-off backfill
```

It is idempotent — re-running it *is* the upgrade — and it writes the units for
you (systemd `--user` on Linux, a launchd agent on macOS).

### Layout

Four directories, four lifetimes. An upgrade replaces the first and touches
nothing else:

| Path | Holds | On upgrade |
|:--|:--|:--|
| `~/.local/share/tokentab/` | `tokentab.py`, `web/dist/`, `VERSION` | replaced wholesale |
| `~/.config/tokentab/` | `rates.json`, `plans.json` | `rates.json` refreshed from the repo; **`plans.json` never overwritten** |
| `~/.local/share/tokentab/tokentab.db` | the store | untouched |
| `~/.local/state/tokentab/` | scan state, collector log | untouched |

`VERSION` records the deployed `git describe` and timestamp, so a host can be
diffed against the repo instead of taken on trust.

Run from a checkout with no install, the repo directory *is* the config
directory — `./tokentab.py report` works with nothing set up.

## Commands

```sh
tokentab scan                 # NDJSON of new events since the last run (byte-offset incremental)
tokentab backfill             # scan everything on disk, ignoring saved offsets
tokentab push --server <host>
tokentab ingest               # NDJSON on stdin -> SQLite
tokentab adopt [--apply]      # stamp accounts onto pre-account events (dry run by default)
tokentab serve --bind <addr> --port 8899
tokentab report --preset cycle [--project foo] [--provider anthropic] … [--json]
tokentab statusline           # one line of current spend, for a prompt or status bar
tokentab verify               # acceptance checks
```

`tokentab report --json` emits the same summary the dashboard renders — headline,
plans, per-dimension breakdowns, daily series and notes — for scripting against.

`tokentab statusline` prints one line: today's value, the current cycle's value
against its cash, and the ratio between them.

```
$68.86 today · $1,369.51 of $616.21 · 2.22×
```

Always the current billing cycle — a cycle's cash is the whole fee no matter how
much of it has elapsed, so the same line over any other window would compare a
slice of usage against a full month. Ask about other windows with `report --json`.

It opens the store read-only, so it will not create one at a mistyped path,
change a schema, build an index inside your prompt, or roll back the journal of
an `ingest` you just killed. It waits about 200 ms for a store a write is
holding, and prints nothing at all unless it can print the whole line: a
missing, corrupt, or mid-write store leaves the prompt alone rather than
painting an error over it on every render. A store older than your tokentab
also goes blank, until the next command that writes migrates it. Set
`TOKENTAB_DEBUG=1` to see why a line is blank.

(The one thing a read-only open still writes is the `-shm` file of a store in
WAL mode, which sqlite requires to read one at all. tokentab never turns WAL on;
if you have, note that a WAL store in a read-only directory cannot be read.)

`tokentab verify` checks that per-project allocations sum back to the allocatable
plan fees over a whole cycle, and re-prices three models by hand against list rates.

## Configuration

| File | What |
|:--|:--|
| `rates.json` | Published list prices per million tokens. Value is computed *at query time*, so correcting a rate re-prices all history. Sources are recorded in the file. |
| `plans.json` | Flat-plan templates (`monthly_usd`, `cycle_day`, `active_from`/`active_to`), per-account overrides, host display names, attribution roots. **Gitignored** — it names your accounts. Start from `plans.example.json`. |

Environment overrides: `TOKENTAB_DB`, `TOKENTAB_STATE`, `TOKENTAB_CONFIG`,
`TOKENTAB_WEB`, `TOKENTAB_HOST`, `TOKENTAB_SERVER`, `TOKENTAB_BIND`, `TOKENTAB_PORT`.

## Architecture

- **Collector + server:** `tokentab.py`, one file, Python 3.10+, **stdlib only**.
  No pip install, no virtualenv, no service dependencies.
- **Store:** SQLite. Content-hash primary key, so ingest is idempotent.
- **Dashboard:** a React SPA in `web/`, built to static files that `tokentab.py`
  serves directly. **The serving host never needs Node** — only the machine that
  builds does. Styling comes from the 42labs design system: adopted shadcn/ui
  primitives, semantic tokens only.

## The demo

```sh
cd web && npm ci && npm run build:demo   # -> web/dist/, no server needed
```

`build:demo` runs `tools/make-demo.py` first. That script invents a fleet —
accounts, projects, machines, three subscriptions including a cancelled one —
and then asks **the real engine** for one summary per view the demo can reach:
each preset, alone and with each single filter. The demo is a lookup over those
answers, so there is no second implementation of the pricing or allocation rules
to drift out of step with the product.

Two filters at once are not precomputed (the matrix squares); the demo drops the
extras and says so on the page. Filter combinations aside, every number in the
demo is what tokentab would actually report for that data.

The dataset is generated, not committed, because the presets are relative to
today — a fixture baked in June would open on an empty billing cycle. The
production build (`npm run build`) never includes it.

## Known limits

* **Flat plans are allocated, never measured.** Stated in the UI wherever it matters.
* **Local models have no list price.** Their Value uses a reference rate for a
  comparable hosted model (`rates.json` → `local_fallback`), flagged in the UI.
* **llama-swap usage is day-resolution and not project-attributed.** llama.cpp
  timing lines carry no timestamp and the proxy sees no working directory, so
  tokens are stamped at collection time and bucketed to `ad-hoc`. Traffic that
  reaches a local model *through* Claude Code is fully attributed; only direct
  llama-swap clients (Zed, the web UI) land here.
* **Metered API keys are modelled but not yet wired.** The schema carries
  `billing='metered'` with a real `cash_usd` per event; nothing populates it yet.
* **Vendor retention beats backfill.** Claude Code prunes its own transcripts
  (2.1 GB → 1.9 GB over a single afternoon during this build). The store is the
  durable record — what is not collected before a prune is gone. Run the collector
  hourly and treat the database as the thing worth backing up.
* **Fees are what you tell it.** Nothing reads your invoices. A wrong
  `monthly_usd` or `cycle_day` scales every cash and allocated figure.

## Contributors

<!-- contributors:start -->
<a href="https://github.com/42piratas" title="42piratas"><img src="https://avatars.githubusercontent.com/u/18232600?v=4&s=64" width="64" height="64" alt="42piratas" /></a>
<!-- contributors:end -->

## License

Open source — [AGPL-3.0](LICENSE). Commercial — contact ahoy@42labs.io.

---
If it earned its keep, [coffee is appreciated](https://buymeacoffee.com/42piratas). ☕
