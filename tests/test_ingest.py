"""v0.5: glyph-collect output -> fleet directory -> analysis, on a hand-made two-node sample."""
import csv
import json
import tarfile
from pathlib import Path

from glyphrca.ingest import (ingest, kernel_xids, parse_nvidia_smi_q, pcie_tree, snapshot_rows)
from glyphrca.report import analyse_fleet

SMI = """
==============NVSMI LOG==============

Timestamp                                 : Mon Oct 19 13:00:00 2026
Driver Version                            : 550.90.07

Attached GPUs                             : 2
GPU 00000000:18:00.0
    Product Name                          : NVIDIA H100 80GB HBM3
    GPU UUID                              : GPU-aaaaaaaa-0000-0000-0000-000000000001
    Serial Number                         : REDACTED
    Remapped Rows
        Correctable Error                 : 0
        Uncorrectable Error               : {u0}
        Pending                           : No
        Remapping Failure Occurred        : No
        Bank Remap Availability Histogram
            Max                           : 640 bank(s)
            High                          : 0 bank(s)
            Partial                       : 0 bank(s)
            Low                           : {low}
            None                          : 0 bank(s)
    Temperature
        GPU Current Temp                  : 35 C

GPU 00000000:2A:00.0
    Product Name                          : NVIDIA H100 80GB HBM3
    GPU UUID                              : GPU-aaaaaaaa-0000-0000-0000-000000000002
    Remapped Rows
        Correctable Error                 : 0
        Uncorrectable Error               : 0
        Pending                           : No
        Remapping Failure Occurred        : No
        Bank Remap Availability Histogram
            Max                           : 640 bank(s)
            High                          : 0 bank(s)
            Partial                       : 0 bank(s)
            Low                           : 0 bank(s)
            None                          : 0 bank(s)
"""

PCI = "\n".join([
    "bdf\tparent\tclass\tvendor\tcur_width\tmax_width\tcur_speed\tmax_speed\tslot",
    "0000:00:00.0\t-\t0x060000\t0x8086\t?\t?\t?\t?\t-",
    "0000:16:01.0\t-\t0x060400\t0x8086\t16\t16\t32.0 GT/s PCIe\t32.0 GT/s PCIe\t-",
    "0000:17:00.0\t0000:16:01.0\t0x060400\t0x1000\t16\t16\t32.0 GT/s PCIe\t32.0 GT/s PCIe\t-",
    "0000:18:00.0\t0000:17:00.0\t0x030200\t0x10de\t{w}\t16\t32.0 GT/s PCIe\t32.0 GT/s PCIe\t3",
    "0000:2a:00.0\t0000:17:00.0\t0x030200\t0x10de\t16\t16\t32.0 GT/s PCIe\t32.0 GT/s PCIe\t4",
    "0000:2b:00.0\t0000:17:00.0\t0x020000\t0x15b3\t16\t16\t16.0 GT/s PCIe\t16.0 GT/s PCIe\t-",
    "0000:00:1f.0\t-\t0x060100\t0x8086\t?\t?\t?\t?\t-",
]) + "\n"

DIAG = {"DCGM GPU Diagnostic": {"test_categories": [
    {"category": "Hardware", "tests": [
        {"name": "Diagnostic", "results": [{"gpu_id": "0", "status": "Fail"}, {"gpu_id": "1", "status": "Pass"}]}]}]}}

DISCOVERY = """+--------+----------------------------------------------------------------------+
| GPU ID | Device Information                                                   |
+--------+----------------------------------------------------------------------+
| 0      | Name: NVIDIA H100 80GB HBM3                                          |
|        | PCI Bus ID: 00000000:18:00.0                                         |
+--------+----------------------------------------------------------------------+
| 1      | Name: NVIDIA H100 80GB HBM3                                          |
|        | PCI Bus ID: 00000000:2A:00.0                                         |
+--------+----------------------------------------------------------------------+
"""


def _node(root: Path, name: str, *, u0="0", low="0 bank(s)", w="16", xid48=False, diag=False):
    d = root / name
    (d / "bmc").mkdir(parents=True)
    (d / "meta.txt").write_text(f"collector_version=0.5.0\nnode={name}\ncollected_utc=2026-10-19T13:00:00Z\n")
    (d / "nvidia-smi-q.txt").write_text(SMI.format(u0=u0, low=low))
    (d / "pci-sysfs.tsv").write_text(PCI.format(w=w))
    k = "2026-10-18T02:00:00+0000 node kernel: Linux version 6.8.0\n"
    if xid48:
        k += ("2026-10-18T03:14:15+0000 node kernel: NVRM: Xid (PCI:0000:18:00): 48, pid=4242, name=python, "
              "An uncorrectable double bit error (DBE) has been detected on GPU in the framebuffer at partition 1.\n"
              "2026-10-18T03:14:16+0000 node kernel: NVRM: Xid (PCI:0000:18:00): 63, pid=4242, name=python, "
              "Row Remapper: New row (0x0000000000012345) marked for remapping, reset gpu to activate.\n")
    (d / "kernel.log").write_text(k)
    (d / "sel.txt").write_text("   1 | 10/18/2026 | 03:14:10 | Power Supply #0x62 | Power Supply AC lost | Asserted\n")
    (d / "bmc" / "README.txt").write_text("instructions")
    if diag:
        (d / "dcgm-diag.json").write_text(json.dumps(DIAG))
        (d / "dcgm-discovery.txt").write_text(DISCOVERY)
    return d


def test_nvidia_smi_parse():
    g = parse_nvidia_smi_q(SMI.format(u0="2", low="3 bank(s)"))
    assert [x["bdf"] for x in g] == ["0000:18:00.0", "0000:2a:00.0"]
    assert g[0]["model"] == "NVIDIA H100 80GB HBM3" and g[0]["uuid"].endswith("0001")
    assert g[0]["remap_uncorr"] == 2 and g[0]["bank_low"] == 3 and g[0]["remap_pending"] == 0
    assert g[1]["remap_uncorr"] == 0 and g[1]["bank_low"] == 0


def test_snapshot_not_doubled_with_kernel_xid():
    g = parse_nvidia_smi_q(SMI.format(u0="1", low="0 bank(s)"))
    xids = kernel_xids("NVRM: Xid (PCI:0000:18:00): 63, pid=1, Row Remapper")
    assert ("0000:18:00", 63) in xids
    assert snapshot_rows(g, "2026-10-19T13:00:00Z", xids) == []
    rows = snapshot_rows(g, "2026-10-19T13:00:00Z", set())
    assert [r["field"] for r in rows] == ["DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS"]


def test_pcie_tree_keeps_paths_to_endpoints_only():
    lines = pcie_tree(PCI.format(w="8"))
    kinds = {l.split()[0]: l.split()[2] for l in lines}
    assert kinds == {"0000:16:01.0": "rootport", "0000:17:00.0": "switch", "0000:18:00.0": "gpu",
                     "0000:2a:00.0": "gpu", "0000:2b:00.0": "nic"}
    assert "0000:18:00.0 0000:17:00.0 gpu 3 x8/x16 gen5/gen5" in lines


def test_end_to_end_from_archives(tmp_path):
    src = tmp_path / "src"
    for name, kw in (("node-a", dict(xid48=True, u0="1", diag=True)), ("node-b", dict(low="2 bank(s)"))):
        d = _node(src, name, **kw)
        with tarfile.open(tmp_path / f"glyph-collect-{name}.tar.gz", "w:gz") as t:
            t.add(d, arcname=name)
    fleet = tmp_path / "fleet"
    notes = ingest([str(p) for p in sorted(tmp_path.glob("*.tar.gz"))], str(fleet))
    assert {n["host"] for n in notes} == {"node-a", "node-b"}
    for f in ("kernel.log", "sel.log", "pcie.txt", "diag.csv"):
        assert (fleet / "node-a" / f).exists(), f
    assert not (fleet / "node-a" / "dcgm.csv").exists()     # remap already in the kernel log as Xid 63
    with open(fleet / "gpus.csv") as f:
        assert len(list(csv.DictReader(f))) == 4

    r = analyse_fleet(str(fleet))
    score = {(g["host"], g["bdf"]): g for g in r["gpu_health"]}
    a0 = score[("node-a", "0000:18:00.0")]
    assert a0["score"] <= 10                                  # failed matrix-math diagnostic caps at 10
    assert score[("node-a", "0000:2a:00.0")]["score"] >= 90
    assert score[("node-b", "0000:18:00.0")]["score"] < score[("node-b", "0000:2a:00.0")]["score"]


def test_unsafe_archive_rejected(tmp_path):
    import io
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as t:
        data = b"x"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    try:
        ingest([str(bad)], str(tmp_path / "fleet"))
    except ValueError as e:
        assert "unsafe" in str(e)
    else:
        raise AssertionError("path traversal not caught")


def test_xid64_is_a_recording_failure_not_exhausted_spares(tmp_path):
    """Xid 64 = the remap could not be written to the InfoROM (reboot; RMA if it repeats).
    Spare rows used up is the separate row-remapping failure flag."""
    src = tmp_path / "src"
    d = _node(src, "node-c")
    (d / "kernel.log").write_text(
        "2026-10-18T03:14:15+0000 node kernel: NVRM: Xid (PCI:0000:18:00): 48, pid=1, name=python, DBE\n"
        "2026-10-18T03:14:16+0000 node kernel: NVRM: Xid (PCI:0000:18:00): 64, pid=1, name=python, "
        "Row remapper recording failure.\n")
    fleet = tmp_path / "fleet"
    ingest([str(d)], str(fleet))
    r = analyse_fleet(str(fleet))
    g = next(x for x in r["gpu_health"] if x["bdf"] == "0000:18:00.0")
    assert "GPU.MEM.REMAP.RECORD_FAIL" in g["evidence"]
    assert 10 < g["score"] <= 74                      # one uncorrectable incident: caution, not "do not sell"
    assert not any("exhausted" in v or "used up" in v for v in g["verdicts"])
