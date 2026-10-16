"""Run the whole pipeline over a fleet directory and produce a report.

Fleet directory layout:
    <fleet>/<host>/sel.log     raw SEL (binary hex records and/or text in any dialect)
    <fleet>/<host>/kernel.log  optional Linux kernel log (dmesg -T or journalctl -o short-iso)
    <fleet>/<host>/dcgm.csv    optional GPU telemetry: timestamp,gpu,field,value
    <fleet>/<host>/diag.csv    optional active diagnostics results: timestamp,gpu,test,result
    <fleet>/<host>/pcie.txt    optional PCIe tree
    <fleet>/inventory.csv      optional: host,fw,bios,...
    <fleet>/gpus.csv           optional: host,bdf,uuid,model,hours (GPU identity for the health index)

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import time
import zlib
from collections import Counter
from pathlib import Path

from . import health, pcie
from .encode import Codebook, Encoder
from .fleet import (build_fleet, crashed, escalations, inventory_join, lemon_nodes, load_inventory,
                    orphan_cleanups, simultaneity, software_wide, zombie_gpus, aer_storms)

TS_BYTES, ID_BYTES = 4, 1


def _argb(args) -> int:
    return sum(len(a) for a in args) + 1


def _bdf_of(e) -> str:
    return next((a[4:] for a in e.args if a.startswith("bdf=")), "?")


def analyse_fleet(fleet_dir: str | Path, codebook: Codebook | None = None, gzip_baseline: bool = True) -> dict:
    fleet_dir = Path(fleet_dir)
    cb = codebook or Codebook.load()
    enc = Encoder(cb)
    t0 = time.perf_counter()

    events, trees, raw_bytes, gz_bytes, lines = {}, {}, 0, 0, 0
    l1_bytes = l2_bytes = 0
    unknown_words: Counter = Counter()
    by_source: Counter = Counter()
    kernel_ignored = 0
    for d in sorted(p for p in fleet_dir.iterdir() if p.is_dir()):
        ev = []
        for name, source in (("sel.log", "sel"), ("kernel.log", "kernel"), ("dcgm.csv", "telemetry"), ("diag.csv", "diag")):
            f = d / name
            if not f.exists():
                continue
            data = f.read_bytes()
            raw_bytes += len(data)
            if gzip_baseline:
                gz_bytes += len(zlib.compress(data, 6))
            got = enc.encode_file(f, source)
            if source == "kernel":
                kernel_ignored += sum(1 for l in data.splitlines() if l.strip()) - len(got)
            by_source[source] += len(got)
            ev += got
        if ev or (d / "sel.log").exists():
            # one story per server: both sources merged in time order (SEL first on ties)
            ev.sort(key=lambda e: (e.ts is None, e.ts or 0.0, e.source != "sel"))
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
    bus_loss = pcie.bus_loss_evidence(trees, events) if trees else {}
    sw = software_wide(events, cb, exclude=common_mode)
    lemons = lemon_nodes(fv, cb, exclude=common_mode)
    esc = escalations(events, cb)
    zombies = zombie_gpus(events)
    orphans = orphan_cleanups(events)
    storms = aer_storms(events, cb)
    iommu = sorted(h for h, ev in events.items() if any(e.concept == "HOST.IOMMU.FAULT" for e in ev))
    init_fail = sorted(h for h, ev in events.items() if any(e.concept == "GPU.INIT.FAIL" for e in ev))
    telemetry = {}
    for h, ev in events.items():
        for e in ev:
            if e.source == "telemetry":
                telemetry.setdefault(e.concept, {}).setdefault(h, set()).add(_bdf_of(e))
    diag = {}
    for h, ev in events.items():
        for e in ev:
            if e.source == "diag":
                test = next((a[5:] for a in e.args if a.startswith("test=")), "?")
                diag.setdefault(h, []).append({"bdf": _bdf_of(e), "test": test, "concept": e.concept})
    gpus = health.load_gpus(fleet_dir / "gpus.csv") if (fleet_dir / "gpus.csv").exists() else None
    cards = health.gpu_health(events, trees, gpus, common_mode) if (gpus or trees) else []
    fabric = sorted(h for h, ev in events.items() if any(e.concept == "FABRIC.MANAGER.DOWN" for e in ev))
    gpu_unknown = Counter(a for ev in events.values() for e in ev if e.concept == "GPU.XID.OTHER"
                          for a in e.args if a.startswith("xid="))
    elapsed = time.perf_counter() - t0

    def attrs(hosts):
        out = {}
        for a in sorted({k for h in hosts for k in inv.get(h, {})}):
            out[a] = dict(Counter(inv[h].get(a, "") for h in hosts if h in inv))
        return out

    return {
        "fleet": {"servers": len(events), "lines": lines, "seconds": round(elapsed, 2),
                  "by_source": dict(by_source), "kernel_lines_ignored": kernel_ignored},
        "levels": {"raw": raw_bytes, "gzip": gz_bytes, "L1": l1_bytes, "L2": l2_bytes, "L3": l3_bytes, "L4": l4_bytes},
        "unknown_words": dict(unknown_words.most_common(10)),
        "unknown_xid": dict(gpu_unknown.most_common(10)),
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
        "bus_loss": bus_loss,
        "software_wide": [{"concept": c, "hosts": hs, "window_s": round(t1 - t0_, 1), "others": others}
                          for c, hs, t0_, t1, others in sw],
        "lemon_nodes": lemons,
        "escalations": [{"host": h, "bdf": b, "concept": c, "per_day": n, "to": to} for h, b, c, n, to in esc],
        "zombies": {h: {"trigger": c, "bdf": b} for h, (c, b) in zombies.items()},
        "orphan_cleanups": [{"host": h, "bdf": b} for h, b in orphans],
        "telemetry": {c: {h: sorted(b) for h, b in hs.items()} for c, hs in telemetry.items()},
        "fabric_manager_down": fabric,
        "aer_storms": storms,
        "diagnostics": diag,
        "iommu_faults": iommu,
        "gpu_init_failures": init_fail,
        "gpu_health": cards,
        "gpu_health_by_model": health.fleet_stats(cards) if cards else {},
        "triage": {c: {"triage": cb.triage(c), "note": cb.concepts.get(c, {}).get("note", "")}
                   for c in sorted({e.concept for ev in events.values() for e in ev}) if c in cb.concepts},
    }


def render_text(r: dict) -> str:
    L = r["levels"]
    raw = L["raw"] or 1
    src = r["fleet"].get("by_source", {})
    out = [f"Fleet: {r['fleet']['servers']:,} servers, {r['fleet']['lines']:,} events "
           f"({', '.join(f'{k}: {v:,}' for k, v in src.items())}; "
           f"{r['fleet'].get('kernel_lines_ignored', 0):,} routine kernel lines ignored), "
           f"analysed in {r['fleet']['seconds']} s", "",
           f"{'Level':<30}{'bytes':>15}{'ratio':>11}"]
    names = {"raw": "L0 raw text", "gzip": "   gzip baseline (no meaning)", "L1": "L1 word glyphs + args",
             "L2": "L2 concept + args", "L3": "L3 runs: concept x count", "L4": "L4 fleet signatures"}
    for k, label in names.items():
        if L.get(k):
            out.append(f"{label:<30}{L[k]:>15,}{raw / L[k]:>10,.0f}x")
    if r["unknown_words"]:
        out.append(f"\nData gaps (words not in the codebook): {r['unknown_words']}")
    if r.get("unknown_xid"):
        out.append(f"Data gaps (Xid codes not in the codebook): {r['unknown_xid']}")
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
    if r.get("bus_loss"):
        out.append("\nDevice left the bus (one event, several witnesses):")
        for path, hosts in sorted(r["bus_loss"].items(), key=lambda kv: -len(kv[1])):
            w = Counter(" + ".join(v) for v in hosts.values())
            out.append(f"  {path:<18} {len(hosts):>4} servers   " + "; ".join(f"{n} by {k}" for k, n in w.items()))
    for s in r.get("software_wide", []):
        out.append(f"\nSoftware-wide: {s['concept']} on {len(s['hosts'])} servers within {s['window_s']} s"
                   f" -> look at the job, driver or library, not at the GPUs"
                   + (f" ({len(s['others'])} other servers outside the window: check them one by one)" if s["others"] else ""))
    if r.get("lemon_nodes"):
        out.append("\nLemon nodes (several hardware failure kinds on one server -> drain and run diagnostics):")
        for h, kinds in sorted(r["lemon_nodes"].items()):
            out.append(f"  {h}: {', '.join(kinds)}")
    if r.get("telemetry"):
        out.append("\nEarly warnings from telemetry (before any Xid):")
        for c, hosts in sorted(r["telemetry"].items()):
            out.append(f"  {c:<26} {len(hosts):>4} servers -> {r['triage'].get(c, {}).get('triage', '')}")
    if r.get("zombies"):
        out.append("\nZombie GPUs (blocked task after the error: soft restarts will hang -> reboot or PCIe hot-plug reset):")
        for h, z in sorted(r["zombies"].items()):
            out.append(f"  {h} {z['bdf']}: {z['trigger']} then OS.HUNG_TASK")
    if r.get("orphan_cleanups"):
        out.append("\nXid 45 with no earlier error on the same GPU (root cause probably lost -> run nvidia-smi -q):")
        for o in r["orphan_cleanups"]:
            out.append(f"  {o['host']} {o['bdf']}")
    if r.get("fabric_manager_down"):
        out.append(f"\nFabric Manager down on {len(r['fabric_manager_down'])} servers -> multi-node jobs will hang "
                   f"at the first collective: drain now: {', '.join(r['fabric_manager_down'])}")
    if r.get("diagnostics"):
        out.append("\nActive diagnostics failures:")
        for h, fails in sorted(r["diagnostics"].items()):
            for f in fails:
                verdict = ("silent data corruption -> RMA, keep out of the cluster" if f["concept"] == "GPU.SDC"
                           else "keep drained, repeat once, RMA if it fails again")
                out.append(f"  {h} {f['bdf']}: {f['test']} failed -> {verdict}")
    if r.get("aer_storms"):
        out.append("\nAER storms (the host may lock up writing logs -> isolate now, rate-limit AER logging):")
        for h, n in sorted(r["aer_storms"].items()):
            out.append(f"  {h}: {n:,} PCIe AER messages in one minute")
    if r.get("iommu_faults"):
        out.append(f"\nIOMMU / DMA remapping faults (GPUDirect traffic stalls, no Xid) -> reboot: {', '.join(r['iommu_faults'])}")
    if r.get("gpu_init_failures"):
        out.append(f"\nGPU driver failed to initialise -> check retirement records, then re-initialise: {', '.join(r['gpu_init_failures'])}")
    if r.get("gpu_health_by_model"):
        out.append("\nGPU health index (0-100 %, from each GPU's own history; not a survival probability):")
        for model, st in r["gpu_health_by_model"].items():
            bands = ", ".join(f"{k} {v}" for k, v in st["bands"].items())
            out.append(f"  {model:<8} {st['gpus']:>6} GPUs   mean {st['mean']:>5} %   median {st['median']:>5} %   {bands}")
        worst = sorted(r["gpu_health"], key=lambda c: c["score"])[:8]
        out.append("  Lowest:")
        for c in worst:
            parts = " · ".join(f"{k} -{v:g}" for k, v in c["breakdown"].items())
            out.append(f"    {c['score']:>5} %  {c['band']:<11} {c['model']:<6} {c['host']} {c['bdf']}  {parts}"
                       + (f"  [{'; '.join(c['verdicts'])}]" if c["verdicts"] else ""))
    if r.get("escalations"):
        out.append("\nEscalations (a light triage repeated too often on one GPU):")
        for e in r["escalations"]:
            out.append(f"  {e['host']} {e['bdf']}: {e['concept']} x{e['per_day']} in 24 h -> {e['to']}")
    return "\n".join(out)
