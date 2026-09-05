# TokenTab

What AI actually costs — across providers, projects and machines, with the flat-rate plans
included rather than excluded. Every usage dashboard you can buy meters API keys, but almost
nobody pays for coding agents that way any more: the spend is a handful of subscriptions, and a
subscription has no per-call price to meter. TokenTab reads the transcripts the CLIs already write
to disk, prices that usage at published list rates, and splits each flat fee across the projects
that consumed it. Live demo at <https://tokentab.42labs.io/> on entirely synthetic data.

**No vendor API key, no proxy in front of your agent, no credentials stored.** That is a design
constraint, not a current limitation.

**Three numbers, and the distinction between them is the product.** *Cash out* is the plan fees
actually paid. *Allocated cost* is a plan's fee split across projects by token share — accounting,
not measurement. *Value* is the same usage priced at list rates. The dashboard never presents an
allocated number as cash: under a filter the card switches figure, turns amber, goes dashed and
carries an `ALLOCATED` badge. Any change that blurs those three breaks the thing the tool exists
to say.

**Open source, AGPL-3.0, passively maintained.** The public `README.md` is the user documentation.

The sibling folder `~/42labs/tokentab-meta/` carries this project's meta side — its `AGENTS.md`,
`docs/` and logs. It is a plain folder, not a repository.

## Crew

The roles this project is worked by, and what each one needs. **No personas live here** — an agent
arrives already knowing who it is, and reads this project to learn the project.

| Role | What this project needs from it |
|------|---------------------------------|
| Engineering | The Python reader and pricing, and the web dashboard |
| Code review | Anything touching allocation, because presenting allocated spend as cash is the one unacceptable defect |
| Data | Rate tables and the transcript formats the CLIs write, which change without notice |
| Sysadmin | The `tokentab.42labs.io` deploy, branch protection and the LGTM gate |

No architect or content role is in use here; positioning and voice live on the meta side.

**After any context loss, re-read your anchor under `~/.agent-anchors/tokentab/`** (canon §17).

## Key files

- `README.md` — user documentation: the three numbers, the allocation basis, install
- `tokentab.py` — the CLI: reads transcripts, prices them, allocates
- `tools/update-rates.py` — refreshes published list rates; `make-demo.py` builds the synthetic demo set
- `web/` — the dashboard (Vite)
- `plans.json` — the operator's real plan fees; `plans.example.json` is the shipped template
- `rates.json`, `price_history.json` — pricing data
- `docs/` — the screenshots the README renders; no prose lives here
