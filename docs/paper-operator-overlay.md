# Paper operator overlay

Status: NOT OPERATIONAL. This note is an install and verification record for a
fail-closed paper overlay. It approves no trade, no CLEAR, no fill, and no
paper run. A policy pass is not a RISK CLEAR.

The package overlays under `src/pumpgrok/config/overlays/` are unchanged.
Nothing in this file is in force until the desk computer sets `PUMPGROK_CONFIG`
to the example file. A checkout on another machine does not update that computer.

## Fixed inputs

These are the human-fixed values. This change does not re-choose them.

| Input | Value |
|---|---|
| Mode | paper only |
| Starting equity | 1000 virtual USDC |
| Max trade | 10 virtual USDC |
| Position ceiling | 5, not applied |
| Runtime position cap | 3 |
| Daily loss halt | 2 percent of verified day-open equity, Asia/Bangkok, latched until a human clears it |
| Drawdown halt | 10 percent of the running peak, latched |

Exit proposals (stop 20, take-profit 60, trail 20, hold 1800s, poll 15s) are
not applied. Package defaults remain 30, 120, 35, 3600, and 15.

## What the engaged overlay enforces

Loading `src/pumpgrok/config/examples/paper-operator.example.toml` as
`PUMPGROK_CONFIG` does all of the following:

- Keeps `execution.mode` at paper and `live.enabled` false.
- Sets the unitless risk fractions that match the delegated policy: trade 0.01,
  token 0.01, daily loss 0.02, drawdown 0.10, 10 trades per day, liquidity
  floor 20, slippage cap 200 bps, open positions 3.
- Leaves `paper_equity_sol` at the package default. That default is not 1000
  virtual USDC. No SOL-to-USDC rate is configured.
- Clears `rpc.endpoints` to an empty list in the merged settings. It does not
  add an endpoint.
- Tightens the safety threshold fields the settings model already has (any
  recorded tax threshold 0, top-10 fail at 20, liquidity multiple min 20,
  creator share veto 0.10).
- Refuses `pumpgrok run`, `paper`, `state`, `killswitch`, `ticket`, `live`,
  `verify`, `backtest`, and `tools` before a desk cycle, a registry build, or
  a ledger genesis write.
- Refuses to build the operator registry and refuses to write
  `initial_equity_lamports` on an empty ledger.
- Refuses buys inside `RiskEngine.check` with reason `paper_policy_blocked`
  and writes no ledger row for that refusal.

The buy refusal lists these blockers:

- `currency_unresolved` (virtual USDC versus the engine SOL ledger; no rate chosen)
- `cost_model_unverified`
- `equity_peak_unverified`
- `halt_day_unaligned` (policy day is Asia/Bangkok; the rollover emitter is still UTC)
- `advisory_blind_scorer_unchanged` (SAFE-4 and SAFE-8 can still be advisory BLIND)
- `blind_flatten_not_authorized` (the adapter path is unchanged and not authorized)
- `pre_migration_excluded`
- `exit_proposals_not_applied`

Flags that would clear those blockers cannot be set true in this schema.

## What this change does not do

- It does not install a wallet, accept a key, or sign.
- It does not scan a market, call an RPC, or open a paper or live fill.
- It does not initialize positions, E0, or a peak.
- It does not change the in-package paper, dev, or live overlays.
- It does not change the UTC `DailyRollover` emitter in the supervisor.
- It does not change `safety.score_report` advisory BLIND handling.
- It does not change `blind_flatten` or the exit rule implementation.
- It does not change `engine/analyzer.py`, where `CREATOR_SHARE_VETO` remains 0.25.
- It does not supply a fee, network, or slippage cost model. The 0.01 fractions
  are unitless caps. They are not an all-in 10 virtual USDC budget, because
  fees and slippage are unverified and the denomination is unresolved.
- It does not make the desk operational, simulated-execution ready, or cleared
  for a paper run.

Package data, Jupiter, and Grok URLs remain in the profile overlay because those
fields must be https URLs. This overlay does not call them. Do not start a run.

`pumpgrok doctor` can be run with the overlay and with the health port left at
0. It prints `NOT OPERATIONAL` and the blockers. Exit 0 means the settings
loaded. It is not a clearance.

## Desk computer

Do this on the existing desk clone. Pulling or editing a different checkout
does not update that clone.

1. Fetch this branch, or apply its patch, in that clone.
2. From the repo root, install the locked dev environment if it is not already
   present: `uv sync --frozen --group dev`. That command does not start the desk.
3. Export `PUMPGROK_CONFIG` as the absolute path of
   `src/pumpgrok/config/examples/paper-operator.example.toml`.
   Leave `PUMPGROK_PROFILE` unset or set it to `paper`.
   Do not set `PUMPGROK_MODE=live`.
   Do not export a wallet, seed, signing, or provider credential.
   Do not set `PUMPGROK_HEALTH__PORT`.
4. Run the offline checks in the next section. They must not be replaced with
   `pumpgrok run`, `pumpgrok paper`, replay, serve, or a market tool.
5. Optional: `uv run pumpgrok doctor` with the same environment. Expect the
   `NOT OPERATIONAL` line. Do not treat a zero exit as permission to trade.

## Offline checks

From the repo root, with `PUMPGROK_CONFIG` unset for the command below (the
tests set it themselves):

```bash
uv run python -m pytest -o addopts= \
  tests/config/test_paper_operator_overlay.py \
  tests/risk/test_paper_policy_blocks_entries.py \
  -q
```

These tests load settings, refuse CLI verbs, and refuse one synthetic buy
inside `RiskEngine.check`. They do not append a fill, do not call a market
or RPC endpoint, and do not start a desk cycle.

## Questions for coordinator YJ

Do not guess these. They stay blocked in this change.

- B1. A sourced SOL-to-USDC valuation rule, or an engine change that accounts
  in virtual USDC. This change chooses neither.
- Whether `max_open_positions` may later rise from 3 to the user ceiling of 5.
- Whether the exit proposals may be applied by a separate human-signed overlay.
- B7. A sourced, dated cost model before any ticket can qualify.
- B4. Whether the supervisor day rollover may move from UTC to Asia/Bangkok.
  Until it does, entries stay blocked even though the halt latch itself does
  not clear at midnight.
- B5. Whether the safety scorer may drop the SAFE-4 and SAFE-8 advisory BLIND
  exception. The scorer was not edited here.
- B8. Whether an automatic blind flatten may exist, and only if it cannot
  close outside 200 bps. It is not authorized, and the adapter was not edited.
- B9. Whether pre-migration bonding-curve tokens get a separate policy. They
  stay excluded.
- B10. A verified ledger E0 and running peak. None was written here.

## Rollback

Unset `PUMPGROK_CONFIG`. The package defaults return. Revert the patch to
remove the gate. No ledger row is required for the rollback because this
change does not write one.
