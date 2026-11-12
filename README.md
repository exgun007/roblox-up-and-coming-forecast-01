# Roblox Up-and-Coming top-10 forecaster

Entry for make.fun's **Predict Roblox's Up-and-Coming top 10**
(`roblox-up-and-coming-forecast-01`).

## The problem

Forecast Roblox's Up-and-Coming chart place-by-place for the 72 hourly reads
after the deadline (2026-11-13T06:00Z → 2026-11-16T06:00Z). Scored by mean
natural-log probability assigned to the actual game at each of the 10 places,
across every hourly read in the window. Higher (closer to 0) is better.

## Method

A seeded Monte-Carlo simulation of rank diffusion, calibrated on the read
history in `/input/history.ndjson`.

1. **Rank diffusion in log space.** Each chart position is a game's rank
   `1..n`. Ranks move multiplicatively, so the simulation evolves `log(rank)`
   and adds an empirical increment drawn from that game's current rank band.
   Increments are estimated from *every* consecutive pair of archive reads,
   rescaled by `1/sqrt(h)` to a per-hour step (rank drift is diffusive over the
   archive's 3.6–11 h gaps; validated below).

2. **Empirical increments, not a Gaussian.** For each rank band the pool of
   observed `dlog(rank)` values is sampled directly. This keeps the heavy tails
   and the asymmetry of real chart moves, which a normal fit smooths away.

3. **Exit hazard per rank band.** A game leaves the chart (ages past Roblox's
   28-day cutoff, or drops off) with a per-hour probability measured from the
   same read pairs. Dead games are pushed to the end of the ordering every
   step, so they stop occupying places.

4. **Monte-Carlo places.** Each of 3000 paths runs the full 72-hour window.
   At every simulated hour the resulting ordering is recorded, giving an
   empirical distribution over which game holds which place, at every hour.

5. **Unnamed-share hedge.** Each place is normalised so the named games carry
   `SUM_FRAC` of the mass; the rest is left unnamed and is shared by every
   game that read's chart has which the entry did not name. This is the
   optimal hedge when the true occupant is a game the simulation never
   surfaced (a newcomer, or a game outside the top 100 cut).

6. **Top 10.** Ranked by summed place-occupancy, ties broken by expected rank,
   which is also the tie-break the grader uses.

## Validation

The scorer was re-implemented from the kit's `grader/forecast/ranked.ts` and
checked against the published house-baseline score to the exact 6 decimals:

```
baseline: log_score -43.512039   hits 6.4   agreement 53.5
```

The chart history for the practice window was reconstructed from the public
archive (`Kenny-Dhub/roblox-snapshots`) at its MIT licence, giving 49 reads
with full contiguous charts — 15 more than the 34 in the kit's practice
history — so the model could be fitted and tested on more than one window.

Walk-forward (fit on the reads before each window, score on the window):

| window   | persistence | model  | house bar |
|----------|-------------|--------|-----------|
| w2       | -37.71      | -28.84 | -37.2     |
| practice | -43.51      | -36.34 | -37.2     |
| w4       | -44.04      | -40.36 | -37.2     |

The model beats persistence on every window by 3.6 to 8.9 nats, and beats the
house baseline bar on the practice window by +0.86 nats. Results are stable
across seeds and path counts (sd < 0.4 nats over 4 seeds × 2 path counts ×
3 SUM_FRAC values; every configuration tested beat the bar).

Two candidate refinements were measured and **rejected** as noise rather than
signal: a super-diffusive rescale (`h^0.744`, fitted) improved the practice
window but degraded the two held-out windows; an age-conditioned exit hazard
using `details.created` moved the practice score by under 0.3 nats in both
directions across windows. Neither is in the shipped model.

## Input parsing

`/input/history.ndjson` is treated as untrusted. A single malformed record must
never abort a run, so the reader validates everything and skips only what it
cannot use:

- **Malformed JSON** — the per-line `json.loads` is wrapped in `try/except`
  (`ValueError`, `TypeError`); the line is counted and skipped. A line that
  parses but is not a JSON object is skipped the same way.
- **Failed reads** — records whose `ok` field is explicitly `false` are skipped.
- **Unusable `up_and_coming`** — skipped when the field is missing, not a
  `list`, `null`, or empty.
- **Unusable rows** — dropped individually, never the whole record: a row that
  is not an object, that has no `universe_id`, whose `universe_id` cannot be
  coerced by `int()` (raising `TypeError`/`ValueError`), or whose `rank` is
  below 1. Rows keep rank order; a record only survives if at least one row
  survives.
- **Unparseable `at`** — the timestamp is validated with `parse_at()` and the
  record is dropped if it fails, since a read with no time cannot be paired.

Skips are silent unless `MAKEFUN_VERBOSE` is set, in which case the reader
prints how many lines and records it dropped.

## Determinism

Reproducibility is structural, not incidental — two runs produce
byte-identical `forecast.json`, and this is verified by md5sum on repeated
runs, both as separate processes and as repeated in-process calls.

- **Single source of randomness.** One module-level `random.Random(SEED)` with
  `SEED = 20261113`. There is no other randomness anywhere in the program: no
  `random.random()`, no hashing of unordered containers, no clock, no
  environment-dependent sampling. `simulate()` re-seeds that one instance from
  `SEED` at entry, so even calling `main()` twice inside a single interpreter
  reproduces the same paths rather than advancing a global stream.
- **Deterministic ordering.** Probability rows are emitted sorted by
  `(place, -count, universe_id)`, so no tie-break can depend on dict insertion
  order. Empirically equal counts resolve by `universe_id`, never by the order
  the histogram happened to be built in.
- **No time, no network.** The program reads only the two directory variables;
  the simulated window is a fixed `HORIZON` of steps from a fixed input, so
  wall-clock time cannot influence the output.

The program reads `$MAKEFUN_INPUT_DIR` / writes `$MAKEFUN_OUTPUT_DIR` when set,
otherwise `/input` and `/output`, and never touches the network.

## Run locally

```sh
mkdir -p out
MAKEFUN_INPUT_DIR=<kit>/input MAKEFUN_OUTPUT_DIR=out python3 main.py
```

Requires Python 3 standard library only.

## Licence

MIT — see `LICENSE`.
