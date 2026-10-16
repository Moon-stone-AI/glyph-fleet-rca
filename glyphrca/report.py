"""Run the whole pipeline over a fleet directory and produce a report.

Fleet directory layout:
    <fleet>/<host>/sel.log     raw SEL (binary hex records and/or text in any dialect)
    <fleet>/<host>/pcie.txt    optional PCIe tree
    <fleet>/inventory.csv      optional: host,fw,bios,...

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import time
import zlib
from collections import Counter
from pathlib import Path

from . import pcie
from .encode import Codebook, Encoder
from .fleet import build_fleet, crashed, inventory_join, load_inventory, simultaneity

TS_BYTES, ID_BYTES = 4, 1


def _argb(args) -> int:
    return sum(len(a) for a in args) + 1


def analyse_fleet(fleet_dir: str | Path, codebook: Codebook | None = None, gzip_baseline: bool = True) -> dict:
    fleet_dir = Path(fleet_dir)
    cb = codebook or Codebook.load()
    enc = Encoder(cb)
    t0 = time.perf_counter()

    events, trees, raw_bytes, gz_bytes, lines = {}, {}, 0, 0, 0
    l1_bytes = l2_bytes = 0
    unknown_words: Counter = Counter()
    for d in sorted(p for p in fleet_dir.iterdir() if p.is_dir()):
        sel = d / "sel.log"
        if sel.exists():
            data = sel.read_bytes()
            raw_bytes += len(data)
            if gzip_baseline:
                gz_bytes += len(zlib.compress(data, 6))
            ev = enc.encode_file(sel)
            events[d.name] = ev
            lines += len(ev)
            for e in ev:
                l1_bytes += TS_BYTES + ID_BYTES * max(1, len(e.words)) + _argb(e.args)
                l2_bytes += TS_BYTES + ID_BYTES + _argb(e.args)
                unknown_words.update(w for w in e.words if w.startswith("?"))
        if (d / "pcie.txt").exists():
            trees[d.name] = pcie.parse_tree(d / "pcie.txt")

    fv = build_fleet(events, cb)
    l3_bytes = sum(ID_BYTES + 4 + TS_BYTES + 2 + sum(len(k) + 2 for k in r.args)
                   for rs in fv.runs.values() for r in rs) + sum(len(w) for w in fv.last_words.values())
    rare = fv.rare()
    l4_bytes = sum(len(s) * ID_BYTES + 2 for s in fv.groups) + 2 * len(fv.runs) + \
        sum(len(fv.last_words[h]) for _, hs in rare for h in hs)

    inv = {}
    inv_path = fleet_dir / "inventory.csv"
    if inv_path.exists():
        inv = load_inventory(str(inv_path))
    cohort = crashed(fv, cb)
    findings = inventory_join(inv, cohort) if inv else []
    spread = simultaneity(fv, cb, cohort)
    common_mode = cohort if (spread is not None and spread < 60 and len(cohort) > 50) else set()
    topo = pcie.analyse(trees, events, exclude=common_mode) if trees else None
    elapsed = time.perf_counter() - t0

    def attrs(hosts):
        out = {}
        for a in sorted({k for h in hosts for k in inv.get(h, {})}):
            out[a] = dict(Counter(inv[h].get(a, "") for h in hosts if h in inv))
        return out

    return {
        "fleet": {"servers": len(events), "lines": lines, "seconds": round(elapsed, 2)},
        "levels": {"raw": raw_bytes, "gzip": gz_bytes, "L1": l1_bytes, "L2": l2_bytes, "L3": l3_bytes, "L4": l4_bytes},
        "unknown_words": dict(unknown_words.most_common(10)),
        "signatures": [{"concepts": list(s), "servers": len(hs), "attributes": attrs(hs)} for s, hs in fv.ranked()],
        "survivors": [{"host": h, "signature": list(s), "last_words": fv.last_words[h]}
                      for s, hs in fv.survivors() for h in hs],
        "rare_other": [{"signature": list(s), "hosts": hs, "example": fv.last_words[hs[0]]}
                       for s, hs in rare if (s, hs) not in fv.survivors()],
        "inventory": [{"attribute": a, "value": v, "crash_rate": round(r, 3), "other_rate": round(o, 3), "servers": n}
                      for a, v, r, o, n in findings],
        "crashed": len(cohort),
        "crash_spread_s": spread,
        "common_mode": bool(common_mode),
        "pcie": {} if topo is None else {
            "missing": topo.missing, "degraded": {p: [h for h, _ in v] for p, v in topo.degraded.items()},
            "upstream_suspects": topo.upstream_suspects, "device_suspects": topo.device_suspects},
    }


def render_text(r: dict) -> str:
    L = r["levels"]
    raw = L["raw"] or 1
    out = [f"Fleet: {r['fleet']['servers']:,} servers, {r['fleet']['lines']:,} SEL lines, "
           f"analysed in {r['fleet']['seconds']} s", "",
           f"{'Level':<30}{'bytes':>15}{'ratio':>11}"]
    names = {"raw": "L0 raw text", "gzip": "   gzip baseline (no meaning)", "L1": "L1 word glyphs + args",
             "L2": "L2 concept + args", "L3": "L3 runs: concept x count", "L4": "L4 fleet signatures"}
    for k, label in names.items():
        if L.get(k):
            out.append(f"{label:<30}{L[k]:>15,}{raw / L[k]:>10,.0f}x")
    if r["unknown_words"]:
        out.append(f"\nData gaps (words not in the codebook): {r['unknown_words']}")
    out.append("\nFleet signatures:")
    for s in r["signatures"]:
        fw = s["attributes"].get("fw", {})
        out.append(f"  {s['servers']:>6} x  {' > '.join(s['concepts'])}" + (f"   fw {fw}" if fw else ""))
    if r["survivors"]:
        out.append("\nSurvivors (rare signatures next to a storm):")
        for s in r["survivors"]:
            out.append(f"  {s['host']}: {' > '.join(s['signature'])}")
            out.append(f"     last words: {s['last_words']}")
    if r.get("rare_other"):
        out.append("\nOther rare signatures (separate small incidents):")
        for s in r["rare_other"]:
            out.append(f"  {len(s['hosts'])} x {' > '.join(s['signature'])}   e.g. {s['example']}")
    if r["crash_spread_s"] is not None:
        out.append(f"\nCrashed servers: {r['crashed']:,}; 90% of them failed within {r['crash_spread_s']:.1f} s"
                   + ("  -> common-mode failure (their PCIe noise is excluded from the tree analysis)"
                      if r["common_mode"] else ""))
    for f in r["inventory"]:
        out.append(f"Inventory: {f['attribute']}={f['value']} -> {f['crash_rate']:.0%} crashed vs "
                   f"{f['other_rate']:.0%} elsewhere ({f['servers']:,} servers). Suspect this value, not the parts.")
    p = r["pcie"]
    if p:
        out.append("\nPCIe tree:")
        for k, label in (("missing", "device missing"), ("degraded", "link degraded"),
                         ("upstream_suspects", "errors on >=2 devices behind one parent -> suspect parent"),
                         ("device_suspects", "errors on one device only -> suspect device")):
            for path, hosts in sorted(p.get(k, {}).items(), key=lambda kv: -len(kv[1])):
                out.append(f"  {label:<58} {path:<18} {len(hosts):>4} servers")
    return "\n".join(out)
