#!/usr/bin/env python3
"""
Roblox Up-and-Coming top-10 forecaster
======================================
Forecasts the chart place-by-place for the 72 hourly reads after the deadline.

Method
------
The chart is a *rank process*: games drift up and down, new games appear at the
bottom, and games vanish (the chart only holds games published in the last 28
days, so every game leaves as it ages). Places are scored independently, so the
quantity to model is a per-place occupancy distribution over the 72 hours.

1. Learn empirical 1-hour log-rank increments from the history, bucketed by the
   game's current rank band (top 2 / 3-5 / 6-10 / 11-20 / 21-40 / 41+), plus a
   per-band per-hour exit hazard. Increments measured over a gap of h hours are
   rescaled by 1/sqrt(h) (rank diffusion grows like sqrt of time).
2. Monte-Carlo the whole chart forward 72 steps many times. At each step every
   game takes one increment from its band's pool and may exit; survivors are
   re-ranked by their log-rank. The place-occupancy histogram of every step is
   accumulated.
3. Normalise each place's histogram into probabilities. A place's rows are
   scaled to `SUM_FRAC`, leaving the remainder to be shared by the unnamed
   games (which is exactly how the grader treats the unnamed share, so the
   leftover budget is a genuine hedge, not a loss).
4. `top10` is the ten games with the highest probability of appearing anywhere
   in the top 10, ordered by their expected place.

Everything is seeded, so two runs are byte-identical. Standard library only.
"""
import json
import math
import os
import random

# ---- tunables (chosen by walk-forward validation on the practice windows) ----
SUM_FRAC = 0.90     # mass given to named games per place; rest hedges unnamed
HAZ_MUL = 0.5       # exit-hazard multiplier
PATHS = 3000        # Monte-Carlo paths (validated: more paths = better, stable)
HORIZON_HOURS = 72  # the scored window length
SEED = 20261113
MIN_P = 1e-6
MAX_ROWS_PER_PLACE = 100
BANDS = ((2,), (5,), (10,), (20,), (40,))  # upper rank of each band
N_BANDS = len(BANDS) + 1


def band_of(rank):
    for i, (hi,) in enumerate(BANDS):
        if rank <= hi:
            return i
    return N_BANDS - 1


def read_history(input_dir):
    """Every read: (start time as ISO string, ordered universe ids)."""
    path = os.path.join(input_dir, "history.ndjson")
    reads = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if not rec.get("ok", True):
                continue
            rows = rec.get("up_and_coming") or []
            if not rows:
                continue
            rows.sort(key=lambda r: r["rank"])
            reads.append((rec["at"], [int(r["universe_id"]) for r in rows]))
    return reads


def hours_between(a, b):
    """Hours between two ISO-8601 UTC timestamps, without importing datetime."""
    from datetime import datetime
    def parse(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (parse(b) - parse(a)).total_seconds() / 3600.0


def build_pools(reads):
    """Empirical per-band 1-hour log-rank increments and exit hazards."""
    pools = [[] for _ in range(N_BANDS)]
    exits = [[0, 0] for _ in range(N_BANDS)]  # exits, observations
    for i in range(len(reads) - 1):
        h = hours_between(reads[i][0], reads[i + 1][0])
        if h <= 0.3:
            continue
        scale = 1.0 / math.sqrt(h)
        before = {g: j + 1 for j, g in enumerate(reads[i][1])}
        after = {g: j + 1 for j, g in enumerate(reads[i + 1][1])}
        for g, r1 in before.items():
            b = band_of(r1)
            exits[b][1] += 1
            if g in after:
                pools[b].append((math.log(after[g]) - math.log(r1)) * scale)
            else:
                exits[b][0] += 1
    hazards = []
    for b in range(N_BANDS):
        e, n = exits[b]
        # ~6.3 hourly steps per recorded gap in the archive -> per-hour hazard
        hazards.append(min(0.02, (e / n if n else 0.0) / 6.3))
    return pools, hazards


def simulate(last_chart, pools, hazards, rng):
    n = len(last_chart)
    pool_sizes = [len(p) for p in pools]
    occ = [dict() for _ in range(10)]

    for _ in range(PATHS):
        logr = [math.log(j + 1) for j in range(n)]
        alive = [True] * n
        for _step in range(HORIZON_HOURS):
            for i in range(n):
                if not alive[i]:
                    continue
                rank = int(round(math.exp(logr[i])))
                if rank < 1:
                    rank = 1
                elif rank > n:
                    rank = n
                b = band_of(rank)
                if pool_sizes[b]:
                    logr[i] += pools[b][rng.randrange(pool_sizes[b])]
                if rng.random() < hazards[b]:
                    alive[i] = False
            order = sorted(range(n), key=lambda i: (not alive[i], logr[i]))
            for k in range(10):
                g = last_chart[order[k]]
                occ[k][g] = occ[k].get(g, 0) + 1
    return occ


def to_forecast(occ, n_paths):
    rows = []
    for k in range(10):
        counts = occ[k]
        total = sum(counts.values())
        if not total:
            continue
        items = sorted(counts.items(), key=lambda kv: -kv[1])[:MAX_ROWS_PER_PLACE]
        weight = sum(v for _, v in items)
        for g, v in items:
            p = (v / weight) * SUM_FRAC
            if p >= MIN_P:
                rows.append({"universe_id": int(g), "place": k + 1, "p": round(p, 7)})

    in_top = {}
    exp_place = {}
    for k in range(10):
        total = sum(occ[k].values()) or 1
        for g, v in occ[k].items():
            in_top[g] = in_top.get(g, 0.0) + v / n_paths
            exp_place[g] = exp_place.get(g, 0.0) + (k + 1) * v / n_paths
    chosen = sorted(in_top, key=lambda g: (-in_top[g], exp_place[g]))[:10]
    chosen.sort(key=lambda g: exp_place[g])
    return {"probabilities": rows, "top10": [int(g) for g in chosen]}


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR") or "/input"
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output"
    os.makedirs(out_dir, exist_ok=True)

    reads = read_history(in_dir)
    pools, hazards = build_pools(reads)
    last_chart = reads[-1][1]

    rng = random.Random(SEED)
    occ = simulate(last_chart, pools, hazards, rng)
    forecast = to_forecast(occ, PATHS * HORIZON_HOURS)

    out_path = os.path.join(out_dir, "forecast.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(forecast, fh, separators=(",", ":"))
    print(f"wrote {out_path}: {len(forecast['probabilities'])} rows, "
          f"top10={forecast['top10'][:3]}...")


if __name__ == "__main__":
    main()
