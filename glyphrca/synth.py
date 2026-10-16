"""Synthetic fleet generator. No real operator data is used anywhere in this project.

Scenarios in one fleet:
  * common-mode crash: every server on firmware 3.2.1 floods its SEL and dies with CATERR at the same
    moment; exactly one of them lives a little longer and records a kernel panic (the survivor);
  * bad riser / switch: a few healthy-firmware servers log PCIe errors on several GPUs behind one switch;
  * bad GPU: a few servers log PCIe errors on a single GPU;
  * missing GPU: a few servers lost a GPU from the PCIe tree (surprise link down);
  * degraded link: a few servers have a GPU trained at x8 instead of x16.
Logs are written in four dialects: ipmitool text, two vendor-style texts, and raw 16-byte SEL hex.

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import csv
import random
import time
from pathlib import Path

CRASH_T = 1_791_900_000  # 2026-10-13 14:00 UTC


def _ts(t):
    return time.strftime("%m/%d/%Y | %H:%M:%S", time.gmtime(t))


# ---------------------------------------------------------------- PCIe layout (same shape everywhere)
LAYOUT = [("RP0", "SW0", ["GPU0", "GPU1", "GPU2", "GPU3"], "NIC0"),
          ("RP1", "SW1", ["GPU4", "GPU5", "GPU6", "GPU7"], "NIC1")]


def make_tree(rng, missing=(), degraded=()):
    """Returns (lines, slot->bdf). Bus numbers are shifted per server, as on real fleets."""
    base, lines, where = rng.choice([0x10, 0x20, 0x40, 0x80]), [], {}
    for i, (rp, sw, gpus, nic) in enumerate(LAYOUT):
        rp_bdf = f"0000:{base + i * 0x10:02x}:01.0"
        sw_bdf = f"0000:{base + i * 0x10 + 1:02x}:00.0"
        lines.append(f"{rp_bdf} - rootport {rp} x16/x16 gen5/gen5")
        lines.append(f"{sw_bdf} {rp_bdf} switch {sw} x16/x16 gen5/gen5")
        where[rp], where[sw] = rp_bdf, sw_bdf
        for j, slot in enumerate(gpus + [nic]):
            bdf = f"0000:{base + i * 0x10 + 2 + j:02x}:00.0"
            where[slot] = bdf
            if slot in missing:
                continue
            kind = "nic" if slot.startswith("NIC") else "gpu"
            w = 8 if slot in degraded else 16
            lines.append(f"{bdf} {sw_bdf} {kind} {slot} x{w}/x16 gen5/gen5")
    return lines, where


# ---------------------------------------------------------------- SEL writers
def _hex(rid, t, st, sn, edir, ed1, ed2, ed3, gid=0x0033):
    b = rid.to_bytes(2, "little") + b"\x02" + int(t).to_bytes(4, "little") + gid.to_bytes(2, "little") + \
        bytes([0x04, st, sn, edir, ed1, ed2, ed3])
    return b.hex()


def _bdf_bytes(bdf):
    _, bus, df = bdf.split(":")
    dev, fn = df.split(".")
    return int(bus, 16), (int(dev, 16) << 3) | int(fn)


class Writer:
    def __init__(self, dialect, rng):
        self.d, self.rng, self.rid, self.lines = dialect, rng, 1, []

    def _emit(self, s):
        self.lines.append(s)
        self.rid += 1

    def boot(self, t):
        if self.d == "hex":
            return
        self._emit({"ipmitool": f"{self.rid:4x} | {_ts(t)} | System Event #0x83 | System Boot Initiated | Asserted",
                    "vendorD": f"Record: {self.rid} Date/Time: {_ts(t)} Severity: Ok Description: System BIOS POST complete.",
                    "vendorH": f"[{self.rid}] {_ts(t)} INFO Server power restored. POST Complete."}[self.d])

    def temp_ok(self, t):
        if self.d == "hex":
            return
        r = self.rng.randint
        self._emit({"ipmitool": f"{self.rid:4x} | {_ts(t)} | Temperature #0x3{r(0, 9)} | Upper Non-critical going high | Deasserted",
                    "vendorD": f"Record: {self.rid} Date/Time: {_ts(t)} Severity: Ok Description: The system inlet temperature is within range.",
                    "vendorH": f"[{self.rid}] {_ts(t)} INFO Temperature Normal (Sensor {r(1, 30)})"}[self.d])

    def mem_corr(self, t):
        r, ch = self.rng.randint, self.rng.randrange(4)
        dimm, sock = r(0, 1), r(0, 1)
        if self.d == "hex":
            return self._emit(_hex(self.rid, t, 0x0C, 0x02, 0x6F, 0xA0, dimm << 4, sock << 4 | ch))
        name = f"{'ABCD'[ch]}{dimm + 1}"
        self._emit({"ipmitool": f"{self.rid:4x} | {_ts(t)} | Memory #0x02 | Correctable ECC | DIMM {name} | Asserted",
                    "vendorD": f"Record: {self.rid} Date/Time: {_ts(t)} Severity: Warning Description: Correctable memory error rate exceeded for DIMM {name}.",
                    "vendorH": f"[{self.rid}] {_ts(t)} CAUTION Corrected Memory Error threshold exceeded (Processor {sock + 1}, Memory Module {name})"}[self.d])

    def pcie_corr(self, t, bdf):
        if self.d == "hex":
            bus, df = _bdf_bytes(bdf)
            return self._emit(_hex(self.rid, t, 0x13, 0x05, 0x71, 0xA0, bus, df))
        self._emit({"ipmitool": f"{self.rid:4x} | {_ts(t)} | Critical Interrupt #0x05 | PCI Express Correctable Error | {bdf} | Asserted",
                    "vendorD": f"Record: {self.rid} Date/Time: {_ts(t)} Severity: Warning Description: A bus correctable error was detected on device {bdf}.",
                    "vendorH": f"[{self.rid}] {_ts(t)} CAUTION PCIe Correctable Error (Device {bdf})"}[self.d])

    def pcie_link_down(self, t, bdf):
        if self.d == "hex":
            bus, df = _bdf_bytes(bdf)
            return self._emit(_hex(self.rid, t, 0x13, 0x04, 0x70, 0xA1, bus, df))
        self._emit({"ipmitool": f"{self.rid:4x} | {_ts(t)} | Critical Interrupt #0x04 | PCI Express Fatal Error | Surprise Link Down | {bdf} | Asserted",
                    "vendorD": f"Record: {self.rid} Date/Time: {_ts(t)} Severity: Critical Description: A fatal error was detected on a component at bus {bdf}: PCIe surprise link down.",
                    "vendorH": f"[{self.rid}] {_ts(t)} CRITICAL PCI Express Fatal Error: Surprise Link Down (Device {bdf})"}[self.d])

    def link_down(self, t):
        r = self.rng.randint
        d = "ipmitool" if self.d == "hex" else self.d  # LAN events are not decoded from binary in v0.1
        self._emit({"ipmitool": f"{self.rid:4x} | {_ts(t)} | LAN #0x0{r(1, 4)} | Link Down | Port {r(1, 2)} | Asserted",
                    "vendorD": f"Record: {self.rid} Date/Time: {_ts(t)} Severity: Critical Description: NIC Integrated Port {r(1, 2)} connection lost.",
                    "vendorH": f"[{self.rid}] {_ts(t)} CRITICAL Network Adapter Link Down (Port {r(1, 2)})"}[d])

    def caterr(self, t):
        cpu = self.rng.randint(1, 2)
        if self.d == "hex":
            return self._emit(_hex(self.rid, t, 0x07, 0x80, 0x03, 0xA1, 0x01, 1 << (cpu - 1), gid=0x0020))
        r = self.rng.randint
        self._emit({"ipmitool": f"{self.rid:4x} | {_ts(t)} | Processor #0x80 | IERR | CPU {cpu} | Asserted",
                    "vendorD": f"Record: {self.rid} Date/Time: {_ts(t)} Severity: Critical Description: CPU {cpu} has an internal error (IERR).",
                    "vendorH": f"[{self.rid}] {_ts(t)} CRITICAL Uncorrectable Machine Check Exception (Processor {cpu}, Bank 0x{r(0, 20):X}, Status 0xBE00000000{r(0, 99999):05d})"}[self.d])

    def panic(self, t):
        if self.d == "hex":
            return self._emit(_hex(self.rid, t, 0x20, ord("K"), 0x6F, 0xA1, ord("e"), ord("r"), gid=0x0021))
        self._emit({"ipmitool": f"{self.rid:4x} | {_ts(t)} | OS Stop / Shutdown #0x01 | Run-time critical stop | Asserted",
                    "vendorD": f"Record: {self.rid} Date/Time: {_ts(t)} Severity: Critical Description: A critical stop occurred during OS run. OS halted.",
                    "vendorH": f"[{self.rid}] {_ts(t)} CRITICAL Operating System failure: Kernel Panic - not syncing: Fatal machine check"}[self.d])


# ---------------------------------------------------------------- fleet
def generate(out: str | Path, bad=500, ok=200, flood=(30, 80), switch_fault=8, gpu_fault=5,
             missing_gpu=6, degraded=3, seed=42) -> dict:
    """Writes a synthetic fleet and returns the ground truth."""
    rng = random.Random(seed)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    hosts = [f"node-{i:05d}" for i in range(bad + ok)]
    healthy = hosts[bad:]
    rng_pick = random.Random(seed + 1)
    special = rng_pick.sample(healthy, switch_fault + gpu_fault + missing_gpu + degraded)
    sw_hosts = special[:switch_fault]
    gpu_hosts = special[switch_fault:switch_fault + gpu_fault]
    miss_hosts = special[switch_fault + gpu_fault:switch_fault + gpu_fault + missing_gpu]
    deg_hosts = special[switch_fault + gpu_fault + missing_gpu:]
    survivor = hosts[rng_pick.randrange(bad)]
    truth = {"survivor": survivor, "bad_fw": "3.2.1", "switch_fault": sw_hosts, "gpu_fault": gpu_hosts,
             "missing_gpu": miss_hosts, "degraded": deg_hosts, "bad_switch": "RP1/SW1",
             "bad_gpu": "RP0/SW0/GPU2", "missing_slot": "RP1/SW1/GPU5", "degraded_slot": "RP0/SW0/GPU1"}

    inv_rows = []
    for i, h in enumerate(hosts):
        broken = i < bad
        w = Writer(rng.choice(["ipmitool", "vendorD", "vendorH", "hex"]), rng)
        tree, where = make_tree(rng, missing=("GPU5",) if h in miss_hosts else (),
                                degraded=("GPU1",) if h in deg_hosts else ())
        t0 = CRASH_T - 6 * 3600
        w.boot(t0)
        for _ in range(rng.randint(2, 5)):
            w.temp_ok(t0 + rng.randint(60, 18000))
        if h in sw_hosts:  # several GPUs behind SW1 complain
            for _ in range(rng.randint(20, 60)):
                w.pcie_corr(t0 + rng.randint(100, 20000), where[rng.choice(["GPU4", "GPU5", "GPU6", "GPU7"])])
        if h in gpu_hosts:  # one GPU complains
            for _ in range(rng.randint(20, 60)):
                w.pcie_corr(t0 + rng.randint(100, 20000), where["GPU2"])
        if h in miss_hosts:
            w.pcie_link_down(t0 + rng.randint(100, 20000), where["GPU5"])
        if broken:
            t = CRASH_T - rng.randint(300, 600)
            for fn, k in ((w.mem_corr, 1), (lambda tt: w.pcie_corr(tt, where[rng.choice(['GPU0', 'GPU3', 'GPU4'])]), 2),
                          (w.link_down, 1)):
                for _ in range(rng.randint(*flood) * k):
                    fn(t)
                    t += rng.random() * 0.2
            die = CRASH_T + rng.uniform(-0.8, 0.8)
            w.caterr(die)
            if h == survivor:
                w.panic(die + 1.7)
        d = out / h
        d.mkdir(exist_ok=True)
        (d / "sel.log").write_text("\n".join(w.lines) + "\n")
        (d / "pcie.txt").write_text("\n".join(tree) + "\n")
        inv_rows.append({"host": h, "fw": "3.2.1" if broken else "3.1.9",
                         "bios": rng.choice(["2.18", "2.19"]), "rack": f"R{rng.randint(1, 40):02d}"})
    with open(out / "inventory.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["host", "fw", "bios", "rack"])
        wr.writeheader()
        wr.writerows(inv_rows)
    return truth
