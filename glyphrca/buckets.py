"""Symptom buckets (v0.4): faulty nodes sorted into work queues by their main symptom.

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

import yaml

from .encode import CODEBOOK_DIR, Codebook, Event

DEFAULT_BUCKETS = CODEBOOK_DIR / "buckets_v0.4.yaml"


def load_buckets(path=DEFAULT_BUCKETS) -> list[dict]:
    return yaml.safe_load(Path(path).read_text())["buckets"]


def _bucket_of(concept: str, buckets: list[dict]) -> str | None:
    for b in buckets:
        if any(concept == m or (m.endswith(".") and concept.startswith(m)) for m in b.get("match", [])):
            return b["name"]
    return None


def bucketize(events_by_host: dict[str, list[Event]], cb: Codebook, common_mode: set[str],
              software_hosts: dict[str, set[str]], lemons: set[str] = frozenset(),
              missing_gpu_hosts: set[str] = frozenset(), buckets: list[dict] | None = None) -> list[dict]:
    """software_hosts: host -> concepts already explained as a software-wide event (not charged to hardware)."""
    buckets = buckets or load_buckets()
    order = {b["name"]: i for i, b in enumerate(buckets)}
    queues: dict[str, list[dict]] = {b["name"]: [] for b in buckets}
    for h, ev in sorted(events_by_host.items()):
        if h in common_mode:
            n = sum(1 for e in ev if cb.severity(e.concept) != "info")
            queues["common_mode"].append({"host": h, "symptoms": {}, "events": n, "critical": 0, "also": [],
                                          "lemon": h in lemons})
            continue
        sw = software_hosts.get(h, set())
        symptoms: Counter = Counter()
        crit = 0
        for e in ev:
            sev = cb.severity(e.concept)
            if sev == "info" or e.concept in sw:
                continue
            symptoms[e.concept] += 1
            crit += sev == "crit"
        if h in missing_gpu_hosts:
            symptoms["GPU.MISSING"] += 1
            crit += 1
        hit = sorted({b for c in symptoms if (b := _bucket_of(c, buckets))}, key=order.get)
        if not hit:
            if sw:
                queues["software"].append({"host": h, "symptoms": {c: 1 for c in sorted(sw)}, "events": len(sw),
                                           "critical": 0, "also": [], "lemon": h in lemons})
            continue
        queues[hit[0]].append({"host": h, "symptoms": dict(symptoms.most_common()), "events": sum(symptoms.values()),
                               "critical": crit, "also": hit[1:] + (["software"] if sw else []),
                               "lemon": h in lemons})
    out = []
    for b in buckets:
        nodes = sorted(queues[b["name"]], key=lambda x: (not x["lemon"], -x["critical"], -x["events"], x["host"]))
        if nodes:
            out.append({"bucket": b["name"], "label": b["label"], "owner": b["owner"], "action": b["action"],
                        "nodes": nodes})
    return out


def write_csv(queues: list[dict], path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bucket", "rank", "host", "lemon", "critical", "events", "symptoms", "also", "owner", "action"])
        for q in queues:
            for i, n in enumerate(q["nodes"], 1):
                w.writerow([q["bucket"], i, n["host"], "yes" if n["lemon"] else "", n["critical"], n["events"],
                            " ".join(f"{c}x{k}" for c, k in n["symptoms"].items()), " ".join(n["also"]),
                            q["owner"], q["action"]])
