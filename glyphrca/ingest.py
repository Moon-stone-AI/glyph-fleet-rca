"""Turn glyph-collect archives into a fleet directory that `analyze` reads (v0.5).

    python -m glyphrca ingest glyph-collect-node-*.tar.gz --out fleet/
    python -m glyphrca analyze fleet/ --health-csv health.csv --buckets-csv queues.csv

Per server the collector gives raw files; here they become the five roads:
    kernel.log                      -> kernel.log (as is)
    sel.txt + bmc/*                 -> sel.log (ipmitool SEL and vendor BMC exports, one text stream)
    nvidia-smi-q.txt                -> gpus.csv rows (host, bdf, uuid, model) and dcgm.csv snapshot rows
                                       for row remapping (pending, failure, uncorrectable rows, bank histogram)
    pci-sysfs.tsv                   -> pcie.txt (root ports, switches and the GPUs, NICs, NVMe under them)
    dcgm-diag.json (+ discovery)    -> diag.csv (EXPERIMENTAL: built without a real customer sample yet)

One incident is never counted twice: if the kernel log already holds Xid 63 (row remap) or Xid 64
(remap failure) for a GPU, the nvidia-smi snapshot of the same fact is not added.

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

GPU_HDR = re.compile(r"^GPU\s+([0-9A-Fa-f]{4,8}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7])\s*$")
KV = re.compile(r"^(\s*)([^:]+?)\s*:\s*(.*?)\s*$")
XID = re.compile(r"NVRM: Xid \(PCI:([0-9A-Fa-f]{4,8}):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})\): (\d+)")
NUM = re.compile(r"\d+")
GEN = {"2.5": 1, "5.0": 2, "8.0": 3, "16.0": 4, "32.0": 5, "64.0": 6}


def bdf(raw: str) -> str:
    """00000000:18:00.0 -> 0000:18:00.0 (lower case)."""
    dom, rest = raw.strip().lower().split(":", 1)
    return f"{int(dom, 16):04x}:{rest}"


def parse_nvidia_smi_q(text: str) -> list[dict]:
    """GPU identity and row-remapping state from `nvidia-smi -q`, one dict per GPU."""
    gpus, cur, section, sec_indent = [], None, None, 0
    for line in text.splitlines():
        m = GPU_HDR.match(line)
        if m:
            cur = {"bdf": bdf(m.group(1))}
            gpus.append(cur)
            section = None
            continue
        if cur is None or not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        kv = KV.match(line)
        if not kv:
            # a section header (a line without " : value")
            name = line.strip()
            if section and indent <= sec_indent:
                section = None
            if name == "Remapped Rows":
                section, sec_indent = "remap", indent
            elif name == "Bank Remap Availability Histogram" and section == "remap":
                section = "hist"
            continue
        key, val = kv.group(2).strip(), kv.group(3).strip()
        if section and indent <= sec_indent:
            section = None
        if key == "Product Name" and "model" not in cur:
            cur["model"] = val
        elif key == "GPU UUID":
            cur["uuid"] = val
        elif section == "remap":
            if key == "Correctable Error":
                cur["remap_corr"] = _int(val)
            elif key == "Uncorrectable Error":
                cur["remap_uncorr"] = _int(val)
            elif key == "Pending":
                cur["remap_pending"] = 1 if val.lower().startswith("yes") else 0
            elif key == "Remapping Failure Occurred":
                cur["remap_failure"] = 1 if val.lower().startswith("yes") else 0
        elif section == "hist" and key in ("Low", "None"):
            cur["bank_" + key.lower()] = _int(val)
    return gpus


def _int(v: str) -> int:
    m = NUM.search(v)
    return int(m.group()) if m else 0


def kernel_xids(text: str) -> set[tuple[str, int]]:
    """(bus-level bdf without function, xid) pairs seen in the kernel log."""
    out = set()
    for dom, bus, dev, x in XID.findall(text):
        out.add((f"{int(dom, 16):04x}:{bus.lower()}:{dev.lower()}", int(x)))
    return out


def snapshot_rows(gpus: list[dict], when: str, xids: set[tuple[str, int]]) -> list[dict]:
    """nvidia-smi state as DCGM-named gauge samples, so the telemetry road scores them."""
    rows = []
    for g in gpus:
        bus = g["bdf"].rsplit(".", 1)[0]
        fields = []
        if g.get("remap_uncorr") and (bus, 63) not in xids:
            fields.append(("DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS", g["remap_uncorr"]))
        elif g.get("remap_pending") and (bus, 63) not in xids:  # one fact, one event
            fields.append(("DCGM_FI_DEV_ROW_REMAP_PENDING", 1))
        if g.get("remap_failure") and (bus, 64) not in xids:
            fields.append(("DCGM_FI_DEV_ROW_REMAP_FAILURE", 1))
        if g.get("bank_low"):
            fields.append(("DCGM_FI_DEV_BANK_REMAP_AVAIL_LOW", g["bank_low"]))
        if g.get("bank_none"):
            fields.append(("DCGM_FI_DEV_BANK_REMAP_AVAIL_NONE", g["bank_none"]))
        rows += [{"timestamp": when, "gpu": g["bdf"], "field": f, "value": v} for f, v in fields]
    return rows


def _kind(cls: str, vendor: str, is_bridge_root: bool) -> str | None:
    c = cls.lower().removeprefix("0x")
    if c.startswith("0604"):
        return "rootport" if is_bridge_root else "switch"
    if vendor.lower() == "0x10de" and c.startswith("03"):
        return "gpu"
    if vendor.lower() == "0x10de" and c.startswith("0680"):
        return "nvswitch"
    if c.startswith("02"):
        return "nic"
    if c.startswith("0108"):
        return "nvme"
    return None


def _gen(speed: str) -> int | None:
    m = re.match(r"\s*([\d.]+)\s*GT/s", speed)
    return GEN.get(m.group(1)) if m else None


def pcie_tree(tsv_text: str) -> list[str]:
    """pci-sysfs.tsv -> pcie.txt lines: the endpoints we care about and every bridge above them."""
    rows = {r["bdf"].lower(): r for r in csv.DictReader(tsv_text.splitlines(), delimiter="\t")}
    keep, out = set(), []
    for b, r in rows.items():
        k = _kind(r["class"], r["vendor"], False)
        if k in ("gpu", "nvswitch", "nic", "nvme"):
            cur = b
            while cur in rows and cur not in keep:
                keep.add(cur)
                cur = rows[cur]["parent"].lower()
    for b in sorted(keep):
        r = rows[b]
        parent = r["parent"].lower()
        parent = parent if parent in keep else "-"
        k = _kind(r["class"], r["vendor"], parent == "-")
        if k is None:
            continue
        g, mg = _gen(r["cur_speed"]), _gen(r["max_speed"])
        w, mw = r["cur_width"], r["max_width"]
        if not (w.isdigit() and mw.isdigit() and g and mg):
            continue  # no link data (virtual function, or a device behind a non-PCIe bus)
        slot = r["slot"] if r.get("slot") and r["slot"] != "-" else f"{k}@{b[5:]}"
        out.append(f"{b} {parent} {k} {slot} x{w}/x{mw} gen{g}/gen{mg}")
    return out


def dcgm_index_map(discovery: str) -> dict[str, str]:
    """dcgmi discovery -l: GPU id -> bdf."""
    out, gid = {}, None
    for line in discovery.splitlines():
        m = re.search(r"^\|?\s*(\d+)\s*\|\s*Name:", line)
        if m:
            gid = m.group(1)
        m = re.search(r"PCI Bus ID:\s*([0-9A-Fa-f]{4,8}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7])", line)
        if m and gid is not None:
            out[gid] = bdf(m.group(1))
            gid = None
    return out


def diag_rows(diag_json: str, when: str, idx: dict[str, str], gpus: list[dict]) -> tuple[list[dict], list[str]]:
    """EXPERIMENTAL. Walk any JSON shape for {name/test, results:[{gpu_id, status}]} objects."""
    notes = []
    try:
        data = json.loads(diag_json)
    except json.JSONDecodeError:
        return [], ["dcgm-diag.json is not JSON: not converted"]
    if not idx:
        idx = {str(i): g["bdf"] for i, g in enumerate(gpus)}
        if idx:
            notes.append("no dcgmi discovery: DCGM GPU ids assumed to follow nvidia-smi order")
    rows = []

    def walk(o):
        if isinstance(o, dict):
            name = o.get("name") or o.get("test_name") or o.get("test")
            res = o.get("results")
            if isinstance(name, str) and isinstance(res, list):
                for r in res:
                    if not isinstance(r, dict):
                        continue
                    status = str(r.get("status", r.get("result", ""))).strip().lower()
                    ids = r.get("gpu_ids") or [r.get("gpu_id", r.get("entity_id"))]
                    for i in ids:
                        if i is None or str(i) not in idx:
                            continue
                        rows.append({"timestamp": when, "gpu": idx[str(i)],
                                     "test": re.sub(r"\W+", "_", name.strip().lower()).strip("_"),
                                     "result": "fail" if status.startswith("fail") else
                                               "pass" if status.startswith("pass") else status or "unknown"})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    if not rows:
        notes.append("dcgm-diag.json: no per-GPU results recognised; please send this file so the reader can be fixed")
    return rows, notes


def _meta(text: str) -> dict[str, str]:
    return dict(l.split("=", 1) for l in text.splitlines() if "=" in l)


def ingest_host(src: Path, fleet: Path) -> dict:
    meta = _meta((src / "meta.txt").read_text()) if (src / "meta.txt").exists() else {}
    host = meta.get("node", src.name)
    when = meta.get("collected_utc", "1970-01-01T00:00:00Z")
    dst = fleet / host
    dst.mkdir(parents=True, exist_ok=True)
    summary = {"host": host, "notes": []}

    kernel = (src / "kernel.log").read_text(errors="replace") if (src / "kernel.log").exists() else ""
    if kernel:
        (dst / "kernel.log").write_text(kernel)
    xids = kernel_xids(kernel)

    sel_parts = []
    if (src / "sel.txt").exists():
        sel_parts.append((src / "sel.txt").read_text(errors="replace"))
    for f in sorted((src / "bmc").glob("*")) if (src / "bmc").is_dir() else []:
        if f.name.lower() != "readme.txt" and f.is_file():
            sel_parts.append(f.read_text(errors="replace"))
    if sel_parts:
        (dst / "sel.log").write_text("\n".join(p.rstrip("\n") for p in sel_parts) + "\n")
    else:
        summary["notes"].append("no BMC event log (no ipmitool SEL, nothing in bmc/)")

    gpus = []
    if (src / "nvidia-smi-q.txt").exists():
        gpus = parse_nvidia_smi_q((src / "nvidia-smi-q.txt").read_text(errors="replace"))
        snap = snapshot_rows(gpus, when, xids)
        if snap:
            with open(dst / "dcgm.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["timestamp", "gpu", "field", "value"])
                w.writeheader()
                w.writerows(snap)
        for g in gpus:
            if g.get("remap_uncorr", 0) > 1:
                summary["notes"].append(f"{g['bdf']}: {g['remap_uncorr']} uncorrectable remapped rows on the card; "
                                        "older incidents may be missing from the kernel log, check by hand")
    summary["gpus"] = [{"host": host, "bdf": g["bdf"], "uuid": g.get("uuid", ""), "model": g.get("model", ""),
                        "hours": ""} for g in gpus]

    if (src / "pci-sysfs.tsv").exists():
        tree = pcie_tree((src / "pci-sysfs.tsv").read_text())
        if tree:
            (dst / "pcie.txt").write_text("\n".join(tree) + "\n")

    if (src / "dcgm-diag.json").exists():
        disc = (src / "dcgm-discovery.txt").read_text() if (src / "dcgm-discovery.txt").exists() else ""
        rows, notes = diag_rows((src / "dcgm-diag.json").read_text(), when, dcgm_index_map(disc), gpus)
        summary["notes"] += notes
        if rows:
            with open(dst / "diag.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["timestamp", "gpu", "test", "result"])
                w.writeheader()
                w.writerows(rows)
    return summary


def _sources(paths: list[str], tmp: Path) -> list[Path]:
    """Archives are unpacked safely into tmp; directories are used as they are."""
    out = []
    for p in map(Path, paths):
        if p.is_dir():
            out += [p] if (p / "meta.txt").exists() or (p / "kernel.log").exists() else \
                   [d for d in sorted(p.iterdir()) if d.is_dir()]
            continue
        with tarfile.open(p) as t:
            base = tmp / p.name
            for m in t.getmembers():
                target = (base / m.name).resolve()
                if not str(target).startswith(str(base.resolve())) or m.issym() or m.islnk():
                    raise ValueError(f"{p}: unsafe path in archive: {m.name}")
            if hasattr(tarfile, "data_filter"):
                t.extractall(base, filter="data")
            else:
                t.extractall(base)
            out += [d for d in sorted(base.iterdir()) if d.is_dir()]
    return out


def ingest(paths: list[str], fleet_dir: str) -> list[dict]:
    fleet = Path(fleet_dir)
    fleet.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="glyph-ingest-"))
    try:
        summaries = [ingest_host(src, fleet) for src in _sources(paths, tmp)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    gpu_rows = [g for s in summaries for g in s.pop("gpus")]
    if gpu_rows:
        gfile = fleet / "gpus.csv"
        old = []
        if gfile.exists():
            with open(gfile, newline="") as f:
                hosts = {g["host"] for g in gpu_rows}
                old = [r for r in csv.DictReader(f) if r["host"] not in hosts]
        with open(gfile, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["host", "bdf", "uuid", "model", "hours"], extrasaction="ignore")
            w.writeheader()
            w.writerows(old + gpu_rows)
    return summaries
