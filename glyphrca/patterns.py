"""Failure patterns (v0.4): from a work queue to a draft entry of the fault reference, and back.

The practitioner's loop, automated where it can be and left to a person where it must be:
  1. take one queue (bucket) of faulty nodes;
  2. mine its failure pattern: which events, in which order, how far apart, on how many GPUs, how it ends;
  3. write a DRAFT record in the C_GPU_Faults format (no hosts, no serials: anonymised by construction);
  4. a practitioner reviews it, gives it a fault code and a fix, and marks it reviewed;
  5. reviewed records form the library; the next analysis labels matching nodes with the known pattern and fix.

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from pathlib import Path

import yaml

from .encode import CODEBOOK_DIR, Codebook, Event

DEFAULT_LIBRARY = CODEBOOK_DIR / "fault_library.yaml"
REPO_URL = "https://github.com/Moon-stone-AI/glyph-fleet-rca"

LAYER = {"gpu_sdc": "GPU", "gpu_off_bus": "GPU", "gpu_memory": "GPU", "gpu_severe": "GPU", "nvlink_fabric": "LINK",
         "pcie_riser": "LINK", "cpu": "HOST", "host_memory": "HOST", "power": "FAC", "thermal": "FAC",
         "network": "FABRIC", "storage": "HOST", "os": "HOST", "software": "SW", "unknown": "FLEET",
         "common_mode": "FLEET"}
# codebook triage words -> the C_GPU_Faults first_action scale
FIRST_ACTION = [("rma", "rma"), ("drain", "drain_and_cordon"), ("cordon", "drain_and_cordon"),
                ("reset_gpu_when_idle", "reset_gpu_when_idle"), ("reset_gpu", "reset_gpu_now"),
                ("reboot", "reboot_node"), ("pcie_reset", "reboot_node"), ("diagnos", "run_diagnostics"),
                ("restart_app", "restart_app"), ("return_logs", "restart_app"), ("vendor", "escalate_vendor")]
IDENTIFYING = re.compile(r"\bnode-\d+|\b\d{1,3}(\.\d{1,3}){3}\b|[\w.+-]+@[\w-]+\.[\w.]+|\b[0-9a-f]{4}:[0-9a-f]{2}:"
                         r"[0-9a-f]{2}\.[0-7]\b|\bS/?N[:=]?\s*\w+", re.I)


def _bdf(e: Event) -> str | None:
    return next((a[4:] for a in e.args if a.startswith("bdf=")), None)


def mine(events_by_host: dict[str, list[Event]], hosts: list[str], cb: Codebook) -> dict:
    """The failure pattern of a set of nodes: shared events, typical order and gaps, scope and ending."""
    seqs, firsts, gpus_hit, endings = [], [], [], Counter()
    for h in hosts:
        seen: dict[str, float | None] = {}
        bdfs = set()
        last = None
        for e in sorted(events_by_host.get(h, []), key=lambda e: (e.ts is None, e.ts or 0)):
            if cb.severity(e.concept) == "info":
                continue
            seen.setdefault(e.concept, e.ts)
            if (b := _bdf(e)) and e.concept.startswith(("GPU.", "LINK.", "PCIE.")):
                bdfs.add(b)
            last = e
        seqs.append(tuple(seen))
        firsts.append(seen)
        gpus_hit.append(len(bdfs))
        if last is not None:
            endings["crash" if cb.fatal(last.concept) else "last: " + last.concept] += 1
    n = len(hosts)
    share = Counter(c for s in seqs for c in set(s))
    core = [c for c, k in share.most_common() if k / n >= 0.5]  # in at least half of the nodes
    # typical order of the core events: by median position in each node's sequence
    pos = {c: statistics.median(s.index(c) for s in seqs if c in s) for c in core}
    order = sorted(core, key=lambda c: (pos[c], -share[c]))
    gaps = []
    for a, b in zip(order, order[1:]):
        d = [f[b] - f[a] for f in firsts if f.get(a) is not None and f.get(b) is not None and f[b] >= f[a]]
        gaps.append(round(statistics.median(d)) if d else None)
    multi = sum(1 for g in gpus_hit if g > 1)
    return {"nodes": n, "order": order, "share": {c: round(share[c] / n, 2) for c in share},
            "gaps_s": gaps, "gpus_per_node": round(statistics.mean(gpus_hit), 2) if gpus_hit else 0,
            "multi_gpu_nodes": multi, "endings": dict(endings.most_common(3))}


def split_queue(queue: dict, match_prefixes: list[str]) -> list[list[str]]:
    """One queue often holds several patterns. Group its nodes by the set of queue-specific events they show;
    the largest groups come first."""
    def mine_key(n):
        return tuple(sorted(c for c in n["symptoms"]
                            if any(c == m or (m.endswith(".") and c.startswith(m)) for m in match_prefixes)))
    groups: dict[tuple, list[str]] = {}
    for n in queue["nodes"]:
        groups.setdefault(mine_key(n), []).append(n["host"])
    return [hs for _, hs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))]


def _first_action(triage: str) -> str:
    return next((v for k, v in FIRST_ACTION if k in triage), "run_diagnostics")


def _fmt_gap(s: int | None) -> str:
    if s is None:
        return "?"
    return f"{s} s" if s < 120 else f"{s // 60} min" if s < 7200 else f"{s // 3600} h"


def draft_record(bucket: dict, pattern: dict, cb: Codebook, run_date: str, index: int = 1) -> dict:
    name = bucket["bucket"]
    order = pattern["order"]
    symptoms = []
    for c in order:
        sig = "xid" if c.startswith(("GPU.", "LINK.NVLINK.ERROR")) else \
              "counter" if c in cb.concepts and any(m.get("glyph") == c for m in cb.metrics.values()) else "kernel_log"
        symptoms.append({"signal": sig, "value": c,
                         "note": f"{cb.concepts.get(c, {}).get('desc', c)}; seen on {int(pattern['share'][c] * 100)} % "
                                 "of nodes with this pattern"})
    if not symptoms:
        symptoms = [{"signal": "tool_output", "value": f"bucket={name}", "note": "no event shared by half the nodes"}]
    behaviour = " -> ".join(order) if order else "no common sequence"
    if len(order) > 1:
        behaviour += " (median gaps: " + ", ".join(_fmt_gap(g) for g in pattern["gaps_s"]) + ")"
    blast = "gpu" if pattern["multi_gpu_nodes"] == 0 and LAYER.get(name) == "GPU" else "node"
    triage = cb.triage(order[0]) if order else "observe"
    rec = {
        "fault_code": f"{LAYER.get(name, 'FLEET')}.DRAFT.{name.upper()}_{index}",
        "title": f"Draft pattern {index} from queue '{bucket['label']}': " + (" then ".join(order) or "mixed"),
        "layer": LAYER.get(name, "FLEET"),
        "applies_to": {"notes": "Mined from one fleet run; architectures to be filled by the reviewer"},
        "symptoms": symptoms,
        "detection": [f"GlyphAI Fleet RCA work queue '{name}'", f"Typical sequence: {behaviour}"],
        "triage": {"first_action": _first_action(triage),
                   "steps": [bucket["action"]] + ([f"Codebook triage for {order[0]}: {triage}"] if order else []),
                   "escalate_when": "to be set by the reviewer"},
        "root_causes": [{"cause": "to be confirmed by the reviewer", "likelihood": "unknown"}],
        "blast_radius": blast,
        "co_occurs_with": [c for c, s in sorted(pattern["share"].items(), key=lambda kv: -kv[1])
                           if c not in order and s >= 0.2][:5],
        "field_data": [f"{pattern['nodes']} nodes in the queue; {pattern['multi_gpu_nodes']} with more than one GPU "
                       f"affected; endings: " + ", ".join(f"{k} ({v})" for k, v in pattern["endings"].items())],
        "evidence": [{"kind": "operator_report", "ref": f"GlyphAI Fleet RCA run, {run_date}", "url": REPO_URL}],
        "confidence": "hypothesis",
        "practitioner_notes": None,
        "review": {"status": "draft", "reviewer": None, "date": None},
    }
    leaks = IDENTIFYING.findall(yaml.safe_dump(rec))
    if leaks:
        raise ValueError(f"draft record would leak identifying data: {leaks[:3]}")
    return rec


def load_library(path=DEFAULT_LIBRARY) -> list[dict]:
    """Reviewed records only: drafts never label nodes."""
    p = Path(path)
    if not p.exists():
        return []
    recs = yaml.safe_load(p.read_text()) or []
    return [r for r in recs if r.get("review", {}).get("status") in ("practitioner_reviewed", "published")]


def match(events_by_host: dict[str, list[Event]], library: list[dict]) -> dict[str, list[dict]]:
    """host -> known patterns whose symptom concepts all appear on the host (most specific first)."""
    out: dict[str, list[dict]] = {}
    for h, ev in events_by_host.items():
        have = {e.concept for e in ev}
        hits = []
        for r in library:
            need = {s["value"] for s in r.get("symptoms", [])}
            if need and need <= have:
                hits.append({"fault_code": r["fault_code"], "title": r["title"],
                             "first_action": r["triage"]["first_action"],
                             "fix": r.get("practitioner_notes") or "; ".join(r["triage"]["steps"]),
                             "size": len(need)})
        if hits:
            out[h] = sorted(hits, key=lambda x: -x["size"])
    return out
