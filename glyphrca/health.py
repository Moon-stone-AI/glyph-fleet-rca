"""GPU health index (v0.2.1): one card per GPU, 0-100 %, built from the GPU's own history.

The index describes the past. It is not a probability of survival.
Weights, ceilings, caps and bands live in codebook/health_v0.2.1.yaml.

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import csv
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from .encode import CODEBOOK_DIR, Event

DEFAULT_HEALTH = CODEBOOK_DIR / "health_v0.2.1.yaml"
STORM_NOISE = ("PCIE.",)  # on servers in a common-mode crash, PCIe floods are the crash, not the GPU


def load_config(path=DEFAULT_HEALTH) -> dict:
    return yaml.safe_load(Path(path).read_text())


def load_gpus(path) -> dict[tuple[str, str], dict]:
    """gpus.csv: host,bdf,uuid,model,hours  (uuid is the stable identity; bdf can change)."""
    out = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out[(row["host"], row["bdf"].lower())] = row
    return out


def _bdf(e: Event) -> str | None:
    return next((a[4:] for a in e.args if a.startswith("bdf=")), None)


def band(score: float, cfg: dict) -> str:
    for b in cfg["bands"]:
        if score >= b["min"]:
            return b["label"]
    return cfg["bands"][-1]["label"]


def gpu_health(events_by_host: dict[str, list[Event]], trees: dict, gpus: dict | None,
               common_mode_hosts: set[str], cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_config()
    pen, comps, caps = cfg["penalties"], cfg["components"], cfg["caps"]

    # the list of GPUs: from gpus.csv, else every gpu node of every PCIe tree
    if gpus is None:
        gpus = {}
        for h, t in trees.items():
            for b, n in t.items():
                if n.kind == "gpu":
                    gpus[(h, b)] = {"host": h, "bdf": b, "uuid": "", "model": "unknown", "hours": ""}

    seen: dict[tuple[str, str], list[str]] = defaultdict(list)  # concept sequence per GPU, in time order
    for h, ev in events_by_host.items():
        for e in ev:
            b = _bdf(e)
            if b is None or (h, b) not in gpus:
                continue
            if h in common_mode_hosts and e.concept.startswith(STORM_NOISE):
                continue
            seen[(h, b)].append(e.concept)
    for h, t in trees.items():  # degraded links come from the PCIe tree
        for b, n in t.items():
            if n.kind == "gpu" and (n.width < n.max_width or n.gen < n.max_gen) and (h, b) in gpus:
                seen[(h, b)].append("LINK.DEGRADED")

    cards = []
    for key, meta in sorted(gpus.items()):
        seq = seen.get(key, [])
        counts = Counter(seq)
        runs = Counter(c for i, c in enumerate(seq) if i == 0 or seq[i - 1] != c)
        by_comp = defaultdict(float)
        evidence = {}
        for concept, rule in pen.items():
            n = counts.get(concept, 0)
            if not n:
                continue
            k = {"each": n, "once": 1, "per_run": runs.get(concept, 0)}[rule["count"]]
            pts = min(rule["points"] * k, rule.get("max", rule["points"] * k))
            by_comp[rule["component"]] += pts
            evidence[concept] = n
        try:
            hours = float(meta.get("hours") or 0)
        except ValueError:
            hours = 0.0
        if hours:
            by_comp["age"] += hours / 8760 * cfg["age"]["points_per_year"]
        breakdown = {c: round(min(v, comps[c]["max"]), 1) for c, v in by_comp.items() if v > 0}
        score = 100 - sum(breakdown.values())
        verdicts = []
        for concept, cap in caps.items():
            if counts.get(concept):
                evidence[concept] = counts[concept]
                if score > cap["max_score"]:
                    score = cap["max_score"]
                verdicts.append(cap["verdict"])
        score = round(max(0.0, score), 1)
        cards.append({"host": key[0], "bdf": key[1], "uuid": meta.get("uuid", ""), "model": meta.get("model", "unknown"),
                      "hours": hours, "score": score, "band": band(score, cfg), "breakdown": breakdown,
                      "evidence": evidence, "verdicts": verdicts})
    return cards


def fleet_stats(cards: list[dict], cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    by_model = defaultdict(list)
    for c in cards:
        by_model[c["model"]].append(c)
    out = {}
    for model, cs in sorted(by_model.items()):
        scores = [c["score"] for c in cs]
        out[model] = {"gpus": len(cs), "mean": round(statistics.mean(scores), 1),
                      "median": round(statistics.median(scores), 1),
                      "bands": dict(Counter(c["band"] for c in cs)),
                      "top_deductions": dict(Counter(k for c in cs for k in c["breakdown"] if k != "age").most_common(3))}
    return out


def write_csv(cards: list[dict], path) -> None:
    comps = ["memory", "severe_xid", "links", "throttling", "diagnostics", "age"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["host", "bdf", "uuid", "model", "hours", "score", "band"] + [f"minus_{c}" for c in comps] + ["verdicts"])
        for c in cards:
            w.writerow([c["host"], c["bdf"], c["uuid"], c["model"], int(c["hours"]), c["score"], c["band"]]
                       + [c["breakdown"].get(k, 0) for k in comps] + ["; ".join(c["verdicts"])])
