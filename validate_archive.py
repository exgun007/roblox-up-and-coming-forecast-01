#!/usr/bin/env python3
"""Score entry/main.py's forecast against real held-out charts from the archive.

The kit's input/history.ndjson ends 2026-11-07T20:53Z; the practice window is
2026-11-07T23:00Z .. 2026-11-10T23:00Z. /tmp/roblox-archive is the public
Kenny-Dhub/roblox-snapshots repo (MIT); each commit's data/discover/latest.json
carries every candidate with its explore:up-and-coming rank, i.e. the real
chart at that commit's time. That is genuine out-of-sample truth.

House rule (RULES.txt / grader): per place k, the probability is the row's p if
the game is named there, else (1 - sum of place-k rows) / (games on that read's
chart not named at place k), floored at 1e-4, then ln. A read's score is the sum
over the 10 places; the log score is the mean over reads.
"""
import json
import math
import subprocess
import sys

ARCHIVE = "/tmp/roblox-archive"
FLOOR = 1e-4
PLACES = 10
WIN_FROM = "2026-11-07T23:00:00Z"
WIN_UNTIL = "2026-11-10T23:00:00Z"


def iso_secs(s):
    s = s.replace("Z", "+00:00")
    d, _, c = s.partition("T")
    y, mo, dd = (int(x) for x in d.split("-"))
    c = c.split("+")[0]
    frac = 0.0
    if "." in c:
        c, _, f = c.partition(".")
        frac = float("0." + f)
    hh, mi, ss = (int(x) for x in c.split(":"))
    yy = y - (mo <= 2)
    era = (yy if yy >= 0 else yy - 399) // 400
    yoe = yy - era * 400
    doy = (153 * (mo + (-3 if mo > 2 else 9)) + 2) // 5 + dd - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return (era * 146097 + doe - 719468) * 86400 + hh * 3600 + mi * 60 + ss + frac


def truth_from_commit(rev):
    """The real up-and-coming chart at a commit, or None."""
    try:
        raw = subprocess.run(["git", "-C", ARCHIVE, "show", f"{rev}:data/discover/latest.json"],
                             capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if raw.returncode != 0 or not raw.stdout.strip():
        return None
    d = json.loads(raw.stdout)
    at = d.get("generated_at")
    if not at:
        return None
    ranked = []
    for c in d.get("candidates") or []:
        for s in c.get("sources") or []:
            if s.get("source") == "explore:up-and-coming" and s.get("rank") is not None:
                ranked.append((int(s["rank"]), int(c["universe_id"])))
    if not ranked:
        return None
    ranked.sort()
    return at, [u for _, u in ranked]


def commits():
    out = subprocess.run(["git", "-C", ARCHIVE, "log", "--format=%H", "--all"],
                         capture_output=True, text=True, timeout=120).stdout.split()
    return out


def load_forecast(path):
    fc = json.load(open(path))
    at = {}
    sums = {}
    for r in fc["probabilities"]:
        at.setdefault(r["place"], {})[r["universe_id"]] = r["p"]
        sums[r["place"]] = sums.get(r["place"], 0.0) + r["p"]
    return at, sums, fc["top10"]


def score_read(at, sums, chart):
    total = 0.0
    for k in range(1, min(PLACES, len(chart)) + 1):
        game = chart[k - 1]
        named = at.get(k, {})
        if game in named:
            p = named[game]
        else:
            rest = len({c for c in chart} - set(named))
            left = max(0.0, 1.0 - sums.get(k, 0.0))
            p = 0.0 if rest == 0 else left / rest
        total += math.log(max(FLOOR, p))
    return total


def persistence_forecast(history_path):
    """The house baseline: chart frozen at the history's end."""
    lines = [json.loads(l) for l in open(history_path) if l.strip()]
    lines = [r for r in lines if r.get("ok") is not False and r.get("up_and_coming")]
    chart = [int(r["universe_id"]) for r in sorted(lines[-1]["up_and_coming"], key=lambda x: x["rank"])]
    rows = []
    for place in range(1, PLACES + 1):
        if place > len(chart):
            break
        rows.append({"universe_id": chart[place - 1], "place": place, "p": 0.5})
        if place == 1:
            if len(chart) > 1:
                rows.append({"universe_id": chart[1], "place": 1, "p": 0.3})
            continue
        rows.append({"universe_id": chart[place - 2], "place": place, "p": 0.15})
        if place < len(chart):
            rows.append({"universe_id": chart[place], "place": place, "p": 0.15})
    at, sums = {}, {}
    for r in rows:
        at.setdefault(r["place"], {})[r["universe_id"]] = r["p"]
        sums[r["place"]] = sums.get(r["place"], 0.0) + r["p"]
    return at, sums, chart[:PLACES]


def main():
    hist = sys.argv[1] if len(sys.argv) > 1 else "/home/ubuntu/web3/makefun/kits/roblox/roblox-up-and-coming-forecast-01-kit/input/history.ndjson"
    fcpath = sys.argv[2] if len(sys.argv) > 2 else "/home/ubuntu/web3/makefun/entry/out/forecast.json"

    at, sums, top10 = load_forecast(fcpath)
    p_at, p_sums, p_chart = persistence_forecast(hist)

    lo, hi = iso_secs(WIN_FROM), iso_secs(WIN_UNTIL)
    reads = []
    for rev in commits():
        got = truth_from_commit(rev)
        if not got:
            continue
        stamp, chart = got
        t = iso_secs(stamp)
        if lo <= t <= hi:
            reads.append((stamp, chart))
    reads.sort()
    # the archive can hold the same slot twice; keep one chart per hour
    seen, uniq = set(), []
    for stamp, chart in reads:
        k = stamp[:13]
        if k in seen:
            continue
        seen.add(k)
        uniq.append((stamp, chart))
    reads = uniq

    print(f"held-out reads in the practice window: {len(reads)}")
    if not reads:
        return
    m_scores, p_scores, hits = [], [], []
    for stamp, chart in reads:
        s = score_read(at, sums, chart)
        m_scores.append(s)
        p_scores.append(score_read(p_at, p_sums, chart))
        hits.append(len(set(top10) & set(chart[:PLACES])))
    mean = sum(m_scores) / len(m_scores)
    persist = sum(p_scores) / len(p_scores)
    print(f"  model       log score {mean:.6f}   typical p {math.exp(mean / 10):.6f}")
    print(f"  persistence log score {persist:.6f}   typical p {math.exp(persist / 10):.6f}")
    print(f"  edge {mean - persist:+.6f} nats")
    print(f"  top-10 hits {sum(hits) / len(hits):.2f} / 10")
    print("  per read:", " ".join(f"{s:.1f}" for s in m_scores))


if __name__ == "__main__":
    main()
