#!/usr/bin/env python3
"""Forecast a ranked chart (top 10) from hourly read history.

Reads ``history.ndjson`` from $MAKEFUN_INPUT_DIR (default /input) and writes
``forecast.json`` to $MAKEFUN_OUTPUT_DIR (default /output) with exactly two
keys:

  probabilities  [{universe_id, place, p}] -- p that the game holds that place
  top10          ten universe ids, index 0 = most likely to be at place 1

Method (see README.md for the derivations):

  1. Empirical log-rank increments per rank band, measured on every consecutive
     pair of reads and rescaled by 1/sqrt(h) to a one-hour step.
  2. Per-band, per-hour exit hazard from the same read pairs.
  3. A seeded 3000-path, 72-step Monte-Carlo of the rank diffusion; every step
     of every path contributes to a per-place occupancy histogram.
  4. Histograms -> per-place probabilities, each place normalised to 0.90 of its
     mass so 10% hedges the games the forecast never named.

Python 3 standard library only.  No network, no clock, no environment reads
beyond the two directory variables.
"""
import json
import math
import os
import random

# ---- model constants -------------------------------------------------------
INPUT_DIR = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")

PATHS = 3000              # Monte-Carlo paths
HORIZON = 72              # one-hour steps simulated per path
PLACES = 10               # places recorded per step
SEED = 20261113           # fixed single source of randomness
SUM_FRAC = 0.90           # named mass per place; the rest hedges unnamed games
MAX_ROWS_PER_PLACE = 100  # at most this many rows per place
MIN_P = 1e-6              # rows below this probability are dropped
MIN_GAP_HOURS = 0.3       # read pairs closer than this are skipped
HAZARD_DIVISOR = 6.3      # exits/observations -> per-hour hazard
HAZARD_CAP = 0.02         # per-hour exit hazard ceiling

# Rank bands, by the upper bound of the current rank.
BAND_BOUNDS = (2, 5, 10, 20, 40)
N_BANDS = len(BAND_BOUNDS) + 1  # six bands: <=2, <=5, <=10, <=20, <=40, >40

# THE ONLY source of randomness in this program.  It is seeded from the
# module-level SEED constant and re-seeded at the top of every simulate()
# call, so the whole pipeline is reproducible by construction: same input ->
# byte-identical forecast.json, run after run, process after process.
RNG = random.Random(SEED)


def band_of(rank):
    """Band index for an integer rank (1-based)."""
    for i, hi in enumerate(BAND_BOUNDS):
        if rank <= hi:
            return i
    return N_BANDS - 1


def parse_at(text):
    """ISO-8601 UTC timestamp -> seconds since epoch, without datetime."""
    s = text.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Split the offset: ...T HH:MM:SS[.frac][+HH:MM]
    off = 0
    for i in range(len(s) - 1, 9, -1):
        if s[i] in "+-":
            sign = 1 if s[i] == "+" else -1
            hh = int(s[i + 1:i + 3] or 0)
            mm = int(s[i + 4:i + 6] or 0) if len(s) >= i + 6 else 0
            off = sign * (hh * 3600 + mm * 60)
            s = s[:i]
            break
    date, _, clock = s.partition("T")
    y, mo, d = (int(x) for x in date.split("-"))
    frac = 0.0
    if "." in clock:
        clock, _, f = clock.partition(".")
        frac = float("0." + f)
    hh, mi, ss = (int(x) for x in clock.split(":"))
    days = days_from_civil(y, mo, d)
    return days * 86400 + hh * 3600 + mi * 60 + ss + frac - off


def days_from_civil(y, m, d):
    """Days since 1970-01-01 (Howard Hinnant's algorithm)."""
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _clean_rows(rows):
    """Validated [(rank, universe_id), ...] sorted by rank, or None.

    A row is usable only if it has an integer-coercible rank and
    universe_id.  Any row that fails is dropped on its own; it never
    sinks the record or the run.
    """
    if not isinstance(rows, list) or not rows:
        return None                       # missing / wrong type / empty
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            rank_hint = row.get("rank")
            uid = int(row.get("universe_id"))      # raises on None/garbage
            if rank_hint is None:
                rank = len(clean) + 1               # rank not needed, order is
            else:
                rank = int(rank_hint)
                if rank < 1:
                    continue
        except (TypeError, ValueError):
            continue                                # unusable row: drop it
        clean.append((rank, uid))
    if not clean:
        return None
    clean.sort(key=lambda t: t[0])
    return [uid for _rank, uid in clean]


def read_history(input_dir):
    """Valid reads -> [(timestamp string, [universe_id, ...] in rank order)].

    Defensive by construction: a malformed line, a record marked failed,
    or an unusable row is skipped in isolation.  One bad record can never
    abort the run.
    """
    path = os.path.join(input_dir, "history.ndjson")
    reads = []
    skipped_lines = 0
    skipped_records = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                skipped_lines += 1            # malformed JSON: skip the line
                continue
            if not isinstance(rec, dict):
                skipped_lines += 1
                continue
            if rec.get("ok") is False:        # failed read: skip
                skipped_records += 1
                continue
            ids = _clean_rows(rec.get("up_and_coming"))
            if ids is None:                   # missing / not a list / empty
                skipped_records += 1
                continue
            at = rec.get("at")
            try:
                parse_at(at)                  # reject unusable timestamps
            except (AttributeError, IndexError, TypeError, ValueError):
                skipped_records += 1
                continue
            reads.append((at, ids))
    if skipped_lines or skipped_records:
        if os.environ.get("MAKEFUN_VERBOSE"):
            print(f"read_history: skipped {skipped_lines} bad line(s), "
                  f"{skipped_records} unusable record(s)")
    return reads


def fit(reads):
    """Per-band increment pools and per-band per-hour exit hazards."""
    pools = [[] for _ in range(N_BANDS)]
    observations = [0] * N_BANDS
    exits = [0] * N_BANDS

    for (t0, prev), (t1, nxt) in zip(reads, reads[1:]):
        h = (parse_at(t1) - parse_at(t0)) / 3600.0
        if h <= MIN_GAP_HOURS:
            continue
        scale = 1.0 / math.sqrt(h)
        later_rank = {}
        for i, uid in enumerate(nxt):
            later_rank.setdefault(uid, i + 1)
        for i, uid in enumerate(prev):
            r1 = i + 1
            band = band_of(r1)
            observations[band] += 1
            r2 = later_rank.get(uid)
            if r2 is None:
                exits[band] += 1
            else:
                pools[band].append((math.log(r2) - math.log(r1)) * scale)

    hazards = []
    for band in range(N_BANDS):
        obs = observations[band]
        if obs == 0:
            hazards.append(0.0)
        else:
            hz = (exits[band] / obs) / HAZARD_DIVISOR
            hazards.append(min(hz, HAZARD_CAP))
    return pools, hazards, observations, exits


def simulate(chart):
    """Monte-Carlo the chart forward.

    Returns hist[place][universe_id] counts of every step of every path.
    """
    n = len(chart)
    if n == 0:
        return [{} for _ in range(PLACES)]

    pools, hazards = MODEL["pools"], MODEL["hazards"]
    # Determinism: re-seed the single module-level RNG so repeated calls in
    # one process reproduce the same paths, not just repeated processes.
    RNG.seed(SEED)
    rand = RNG.random

    hist = [{} for _ in range(PLACES)]
    span = max(1, min(PLACES, n))
    pool_lens = [len(p) for p in pools]

    for _ in range(PATHS):
        # alive games, in start order: (universe_id, log_rank)
        state = [(chart[j], math.log(j + 1)) for j in range(n)]
        for _step in range(HORIZON):
            nxt = []
            for uid, log_rank in state:
                rank = int(math.floor(math.exp(log_rank) + 0.5))
                if rank < 1:
                    rank = 1
                elif rank > n:
                    rank = n
                band = band_of(rank)
                ln = pool_lens[band]
                if ln:
                    log_rank += pools[band][int(rand() * ln)]
                if rand() < hazards[band]:
                    continue                      # game exits the chart
                nxt.append((uid, log_rank))
            nxt.sort(key=lambda t: t[1])          # ascending log-rank
            for place in range(min(span, len(nxt))):
                uid = nxt[place][0]
                row = hist[place]
                row[uid] = row.get(uid, 0) + 1
            state = nxt
    return hist


def probabilities_from(hist, total_steps):
    """Keep the 100 largest counts per place, scale the kept mass to 0.90.

    Emission order is fixed by construction -- (place, -count, universe_id) --
    so no tie-break can depend on dict insertion order: the same histogram
    always serialises to the same byte sequence.
    """
    rows = []
    for place in range(PLACES):
        counts = hist[place]
        if not counts:
            continue
        kept = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        kept = kept[:MAX_ROWS_PER_PLACE]
        mass = sum(c for _, c in kept)
        if mass <= 0:
            continue
        for uid, count in kept:
            p = (count / mass) * SUM_FRAC
            if p < MIN_P:
                continue
            rows.append({"universe_id": uid, "place": place + 1, "p": p})
    rows.sort(key=lambda r: (r["place"], -r["p"], r["universe_id"]))
    return rows


def top10_from(hist, total_steps):
    """Highest appearance probability; ties by expected place; then order."""
    appeared = {}
    expected = {}
    for place in range(PLACES):
        for uid, count in hist[place].items():
            appeared[uid] = appeared.get(uid, 0) + count
            expected[uid] = expected.get(uid, 0.0) + (place + 1) * count
    stats = {}
    for uid, count in appeared.items():
        stats[uid] = (count / total_steps, expected[uid] / total_steps)
    best = sorted(stats, key=lambda u: (-stats[u][0], stats[u][1], u))[:PLACES]
    best.sort(key=lambda u: (stats[u][1], u))
    return best


def main():
    reads = read_history(INPUT_DIR)
    if not reads:
        raise SystemExit("no valid reads with a non-empty up_and_coming")

    pools, hazards, observations, exits = fit(reads)
    MODEL["pools"] = pools
    MODEL["hazards"] = hazards

    chart = reads[-1][1]                 # the chart as the history ends
    hist = simulate(chart)

    total_steps = PATHS * HORIZON
    probabilities = probabilities_from(hist, total_steps)
    top10 = top10_from(hist, total_steps)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, "forecast.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"probabilities": probabilities, "top10": top10}, fh)
        fh.write("\n")

    if os.environ.get("MAKEFUN_VERBOSE"):
        for band in range(N_BANDS):
            lo = 1 if band == 0 else BAND_BOUNDS[band - 1] + 1
            hi = BAND_BOUNDS[band] if band < len(BAND_BOUNDS) else "inf"
            pool = pools[band]
            mean = sum(pool) / len(pool) if pool else 0.0
            var = (sum((x - mean) ** 2 for x in pool) / len(pool)) if pool else 0.0
            print(f"band {band} rank {lo}..{hi}: n={len(pool)} "
                  f"obs={observations[band]} exits={exits[band]} "
                  f"hazard={hazards[band]:.6f} mean={mean:+.4f} "
                  f"sd={math.sqrt(var):.4f}")
        print(f"reads={len(reads)} chart_n={len(chart)} rows={len(probabilities)}")
        print("top10:", top10)


MODEL = {}

if __name__ == "__main__":
    main()
