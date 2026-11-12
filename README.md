# Ranked-chart top-10 forecaster

Forecasts a ranked chart's top 10 place-by-place, 72 hourly reads ahead, from
`history.ndjson` in `$MAKEFUN_INPUT_DIR` (default `/input`). Writes
`forecast.json` to `$MAKEFUN_OUTPUT_DIR` (default `/output`) with exactly two
keys:

```json
{"probabilities": [{"universe_id": 10769671316, "place": 1, "p": 0.3745}],
 "top10": [10769671316, 10772879271, 10771743491]}
```

Python 3 standard library only. No network, no clock, no filesystem access
beyond the two directories.

## Method

A seeded Monte-Carlo simulation of rank diffusion, calibrated on the read
history. Every constant below is in `main.py`:

| constant | value | role |
|----------|-------|------|
| `PATHS` | 3000 | Monte-Carlo paths |
| `HORIZON` | 72 | one-hour steps per path |
| `SEED` | 20261113 | the only source of randomness |
| `SUM_FRAC` | 0.90 | named mass per place; 10% hedges unnamed games |
| `MAX_ROWS_PER_PLACE` | 100 | rows kept per place |
| `MIN_P` | 1e-6 | rows below this are dropped |
| `MIN_GAP_HOURS` | 0.3 | read pairs closer than this are skipped |
| `HAZARD_DIVISOR` | 6.3 | exits/observations → per-hour hazard |
| `HAZARD_CAP` | 0.02 | per-hour exit-hazard ceiling |

**1. Rank diffusion in log space.** A game's rank moves multiplicatively, so
the simulation evolves `log(rank)` and adds one empirical increment per step.
Ranks are bucketed into six *bands* by the upper bound of the current rank —
`<=2`, `<=5`, `<=10`, `<=20`, `<=40`, `>40` — because games near the top jostle
for position far more than games deep in the chart (measured: sd 0.16 vs 0.07
log-rank per hour).

**2. Empirical increments, not a Gaussian.** For every consecutive pair of
reads the elapsed hours `h` are measured and pairs with `h <= 0.3` are skipped.
For each game in the earlier read at rank `r1` the band is credited one
observation; if the game is also in the later read at rank `r2` the value

```
(ln(r2) - ln(r1)) / sqrt(h)
```

joins that band's pool. The `1/sqrt(h)` rescale turns a gap of `h` hours into a
one-hour step: rank drift is diffusive, so displacement grows like `sqrt(time)`.
Sampling the observed values directly keeps the heavy tails and asymmetry of
real chart moves that a normal fit would smooth away.

**3. Exit hazard per band.** A game in the earlier read but absent from the
later one counts one exit for its band. The per-hour exit hazard is
`(exits / observations) / 6.3`, capped at `0.02`. Games leave the chart as they
age past the 28-day cutoff, and exits cluster deep in the chart — this run
measured hazard `0.0` in bands 0, 1 and 3 versus `0.0126` in band 5.

**4. Monte-Carlo places.** Each of the 3000 paths starts from the chart as the
history ends: the game at index `j` (0-based) begins at `ln(j+1)`, alive. Each
of 72 hourly steps, for every alive game: recover the integer rank by rounding
`exp(log_rank)`, clamp into `1..n`, look up its band, add one increment drawn
from that band's pool, then mark it dead with probability equal to that band's
hazard. Dead games stay dead and sort to the back. Survivors are then sorted by
ascending `log_rank`, and the universe_id at each of the first ten positions is
recorded into that place's histogram. Histograms accumulate over all 72 steps of
all 3000 paths — 216,000 recorded steps.

**5. Probabilities.** For each place the counts are sorted, at most the 100
largest are kept, and

```
p = (count / sum_of_kept_counts) * 0.90
```

dropping any row below `1e-6`. The `0.90` deliberately leaves 10% of each
place's mass unnamed: the remainder is shared by every game the forecast did not
name at that place, which is the optimal hedge when the true occupant is a
newcomer or a game outside the top-100 cut.

**6. Top 10.** Each game's appearance probability is its summed count over all
ten places divided by total steps, and its expected place is
`sum(place * count) / total_steps`. The ten games with the highest appearance
probability are taken (ties broken by expected place), then ordered by ascending
expected place.

## Results

Run on the kit's 34-read history (2026-10-30T07:36Z → 2026-11-07T20:53Z, a
97-game chart):

```
reads=34  chart_n=97  probability rows=918
total named mass = 9.0  (0.90 at each of the 10 places)
per-place rows: place 1 = 60, places 2..10 = 83..97
max p: place 1 = 0.3745, place 10 = 0.0636
forecast.json = 61,586 bytes
top10: 10769671316, 10772879271, 10771743491, 10771881861, 10770575733,
       10771421858, 10773425091, 10769941703, 10771928551, 10772217708
```

### Held-out validation

`validate_archive.py` scores the forecast on the 10 real chart reads of the
practice window (2026-11-07T23:00Z → 2026-11-10T23:00Z), rebuilt from the
public `Kenny-Dhub/roblox-snapshots` archive (MIT), which records each game's
`explore:up-and-coming` rank per snapshot. These reads are **not** in the
input history, so this is genuine out-of-sample truth.

The scorer is the house rule: per place, the row's `p` if the game is named
there, else `(1 - place sum) / (games not named at that place)`, floored at
`1e-4`, then `ln`; a read's score sums the 10 places, and the log score is the
mean over reads.

```
held-out reads in the practice window: 10
  model       log score -35.947272   typical p 0.027468
  persistence log score -43.512039   typical p 0.012891
  edge        +7.564767 nats
  top-10 hits 6.40 / 10
```

The persistence row is the house baseline (chart frozen where the history
ends). It reproduces the published house-baseline numbers **exactly** —
`-43.512039`, typical probability `0.012891`, `6.4` top-10 hits — which
confirms both the scorer transcription and the truth reconstruction are
correct. Against that verified baseline the model gains **+7.56 nats** on data
it never saw.

The model's advantage grows with horizon, as it should for a diffusion model
over a frozen chart: per-read scores start at `-23.9` on the read nearest the
history's end and reach `-42.7` on the last, while persistence decays faster
throughout.

## Determinism

All randomness flows from one module-level `random.Random(SEED)`, re-seeded at
the top of `simulate()`, so even repeated calls inside one process replay the
same paths. Probability rows are emitted in a fixed order — by place, then
descending count, then `universe_id` — so no tie-break can depend on dict
insertion order. Verified byte-identical (`sha256 521942d0…`) across repeated
runs and across `PYTHONHASHSEED` of 0, 1, 12345 and 99999.

## Robustness

`read_history` never lets one bad record kill the run: malformed JSON lines are
skipped, records with `"ok": false` are skipped, records whose
`up_and_coming` is missing, not a list, or empty are skipped, and any single
row without a usable `rank` or `universe_id` is dropped on its own. Timestamps
are parsed without `datetime` (civil-date arithmetic) and unparseable ones
reject only their record. A history with no usable read exits non-zero with a
clear message rather than writing a malformed forecast.

Edge cases exercised: all-lines-skipped (exits 1, no output file), `ok: false`
interleaved with valid reads, sub-0.3h-only read pairs (hazard pools empty, the
run still completes), and charts shorter than 10 games.

## Run locally

```sh
mkdir -p out
MAKEFUN_INPUT_DIR=<kit>/input MAKEFUN_OUTPUT_DIR=out python3 main.py
```

Set `MAKEFUN_VERBOSE=1` to print the fitted bands, hazards and row count.
Runtime ≈ 12 s, single core, well inside the 300 s budget.

## Licence

MIT — see `LICENSE`.
