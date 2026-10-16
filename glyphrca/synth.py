"""Synthetic fleet generator. No real operator data is used anywhere in this project.

Scenarios in one fleet:
  * common-mode crash: every server on firmware 3.2.1 floods its SEL and dies with CATERR at the same
    moment; exactly one of them lives a little longer and records a kernel panic (the survivor);
  * bad riser / switch: a few healthy-firmware servers log PCIe errors on several GPUs behind one switch;
  * bad GPU: a few servers log PCIe errors on a single GPU;
  * missing GPU: a few servers lost a GPU from the PCIe tree (surprise link down);
  * degraded link: a few servers have a GPU trained at x8 instead of x16;
  * kernel side (v0.2): the crash storm also shows in the kernel (EDAC, AER, MCE); the missing GPU is
    reported by the driver too (Xid 79); an application bug hits Xid 13 on many servers within seconds;
    a few lemon nodes fail in several hardware categories; a few servers log NVLink errors (Xid 74);
    a couple log an Xid the codebook does not know yet.
SEL is written in four dialects (ipmitool text, two vendor-style texts, raw 16-byte hex); the kernel log
in two formats (dmesg -T and journalctl -o short-iso).

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


# ---------------------------------------------------------------- kernel log writer
class KernelWriter:
    def __init__(self, fmt, host, rng):
        self.fmt, self.host, self.rng, self.lines = fmt, host, rng, []

    def line(self, t, msg):
        if self.fmt == "dmesg":
            stamp = time.strftime("[%a %b %d %H:%M:%S %Y]", time.gmtime(t))
            self.lines.append((t, f"{stamp} {msg}"))
        else:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(t))
            self.lines.append((t, f"{stamp} {self.host} kernel: {msg}"))

    def noise(self, t):
        for msg in ("Linux version 6.8.0-45-generic (buildd@lcy02-amd64-075) #45-Ubuntu SMP",
                    "nvidia-nvlink: Nvlink Core is being initialized, major device number 510",
                    "NVRM: loading NVIDIA UNIX x86_64 Kernel Module  560.35.03",
                    "mlx5_core 0000:9a:00.0: firmware version: 28.41.1000"):
            self.line(t, msg)
            t += 1

    def xid(self, t, bdf, code, text):
        self.line(t, f"NVRM: Xid (PCI:{bdf[:-2]}): {code}, pid={self.rng.randint(1000, 99999)}, name=python3, {text}")

    def edac_ce(self, t):
        r = self.rng.randint
        s, mc, c, d = r(0, 1), r(0, 3), r(0, 1), r(0, 1)
        self.line(t, f"EDAC MC{mc}: 1 CE memory read error on CPU_SrcID#{s}_MC#{mc}_Chan#{c}_DIMM#{d} "
                     f"(channel:{c} slot:{d} page:0x{r(4096, 99999):x} offset:0x0 grain:32 syndrome:0x0)")

    def aer_ce(self, t, bdf):
        self.line(t, f"nvidia {bdf}: PCIe Bus Error: severity=Corrected, type=Physical Layer, (Receiver ID)")

    def mce(self, t):
        self.line(t, f"mce: [Hardware Error]: CPU {self.rng.randint(0, 63)}: Machine Check: 0 Bank 5: be00000000800400")

    def panic(self, t):
        self.line(t, "Kernel panic - not syncing: Fatal machine check")

    def text(self):
        return "\n".join(l for _, l in sorted(self.lines, key=lambda x: x[0])) + "\n"


# ---------------------------------------------------------------- fleet
def generate(out: str | Path, bad=500, ok=200, flood=(30, 80), switch_fault=8, gpu_fault=5,
             missing_gpu=6, degraded=3, seed=42, kernel=True, app_bug=40, lemons=3, nvlink=4,
             unknown_xid=2, escalate=1, telemetry=True) -> dict:
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
    rest = [h for h in healthy if h not in set(special)]
    rng_pick.shuffle(rest)
    lemon_hosts, rest = rest[:lemons], rest[lemons:]
    nvl_hosts, rest = rest[:nvlink], rest[nvlink:]
    unk_hosts, rest = rest[:unknown_xid], rest[unknown_xid:]
    esc_hosts, rest = rest[:escalate + 1], rest[escalate + 1:]  # the last one stays below the limit
    def take(n):
        nonlocal rest
        got, rest = rest[:n], rest[n:]
        return got
    zombie_hosts, fm_hosts, orphan_hosts, gsp_hosts = take(1), take(2), take(1), take(1)
    crc_hosts, replay_hosts, spare_hosts, power_hosts = take(3), take(2), take(2), take(2)
    control_hosts = take(10)
    iommu_hosts, storm_hosts, init_hosts = take(1), take(1), take(1)
    sdc_hosts, diagfail_hosts, diagpass_hosts = take(1), take(1), take(3)
    app_hosts = rest[:app_bug]
    truth = {"survivor": survivor, "bad_fw": "3.2.1", "switch_fault": sw_hosts, "gpu_fault": gpu_hosts,
             "missing_gpu": miss_hosts, "degraded": deg_hosts, "bad_switch": "RP1/SW1",
             "bad_gpu": "RP0/SW0/GPU2", "missing_slot": "RP1/SW1/GPU5", "degraded_slot": "RP0/SW0/GPU1",
             "app_bug": app_hosts if kernel else [], "lemons": lemon_hosts if kernel else [],
             "nvlink": nvl_hosts if kernel else [], "unknown_xid": unk_hosts if kernel else [],
             "escalate": esc_hosts[:escalate] if kernel else [], "escalate_slot": "RP0/SW0/GPU3",
             "zombie": zombie_hosts if kernel else [], "fabric_manager": fm_hosts if kernel else [],
             "orphan_xid45": orphan_hosts if kernel else [], "gsp_repeat": gsp_hosts if kernel else [],
             "iommu": iommu_hosts if kernel else [], "aer_storm": storm_hosts if kernel else [],
             "init_fail": init_hosts if kernel else [],
             "sdc": sdc_hosts if kernel else [], "diag_fail": diagfail_hosts if kernel else [],
             "telemetry": {"LINK.NVLINK.CRC_RATE": crc_hosts, "PCIE.REPLAY_RATE": replay_hosts,
                           "GPU.MEM.REMAP.LOW_SPARE": spare_hosts, "POWER.THROTTLE": power_hosts} if telemetry else {}}
    krng = random.Random(seed + 2)  # kernel content has its own stream, so SEL stays as in v0.1
    t_app = CRASH_T - 3 * 3600

    inv_rows = []
    for i, h in enumerate(hosts):
        broken = i < bad
        w = Writer(rng.choice(["ipmitool", "vendorD", "vendorH", "hex"]), rng)
        tree, where = make_tree(rng, missing=("GPU5",) if h in miss_hosts else (),
                                degraded=("GPU1",) if h in deg_hosts else ())
        t0 = CRASH_T - 6 * 3600
        k = KernelWriter(krng.choice(["dmesg", "journal"]), h, krng)
        k.noise(t0)
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
            t_ld = t0 + rng.randint(100, 20000)
            w.pcie_link_down(t_ld, where["GPU5"])
            k.xid(t_ld + 1, where["GPU5"], 79, "GPU has fallen off the bus.")
        if broken:
            t = CRASH_T - rng.randint(300, 600)
            phase = {}
            for name, fn, mult in (("mem", w.mem_corr, 1),
                                   ("pcie", lambda tt: w.pcie_corr(tt, where[rng.choice(['GPU0', 'GPU3', 'GPU4'])]), 2),
                                   ("link", w.link_down, 1)):
                start = t
                for _ in range(rng.randint(*flood) * mult):
                    fn(t)
                    t += rng.random() * 0.2
                phase[name] = (start, t)
            die = CRASH_T + rng.uniform(-0.8, 0.8)
            w.caterr(die)
            if h == survivor:
                w.panic(die + 1.7)
            for _ in range(krng.randint(5, 15)):
                k.edac_ce(krng.uniform(*phase["mem"]))
            for _ in range(krng.randint(5, 15)):
                k.aer_ce(krng.uniform(*phase["pcie"]), where[krng.choice(["GPU0", "GPU3", "GPU4"])])
            k.mce(die - 3)
            if h == survivor:
                k.panic(die + 1.7)
        if h in app_hosts:  # one job crashes on many nodes at once: software, not GPUs
            k.xid(t_app + krng.uniform(0, 30), where[krng.choice(["GPU0", "GPU1", "GPU2", "GPU3"])], 13,
                  "Graphics SM Warp Exception on (GPC 1, TPC 2, SM 0): Out Of Range Address")
        if h in lemon_hosts:  # the same server keeps failing in different ways
            tl = t0 + krng.randint(100, 15000)
            k.xid(tl, where["GPU2"], 48, "DBE (double bit error) ECC error.")
            k.xid(tl + 5, where["GPU2"], 63, "Row remapping event.")
            for j in range(5):
                k.edac_ce(tl + 60 + j)
            for j in range(3):
                k.aer_ce(tl + 120 + j, where["GPU2"])
        if h in nvl_hosts:
            for j in range(krng.randint(2, 5)):
                k.xid(t0 + 500 + j * 7, where["GPU6"], 74, "NVLink: fatal error detected on link 3.")
        if h in zombie_hosts:  # uncontained ECC, then a task stuck in state D
            k.xid(t0 + 3000, where["GPU7"], 95, "Uncontained: FBHUB. RST: Yes, D-RST: No")
            k.line(t0 + 3200, "INFO: task python3:4242 blocked for more than 120 seconds.")
        if kernel and h in sdc_hosts + diagfail_hosts + diagpass_hosts:  # pre-flight dcgmi diag -r 3
            ts_d = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + 1000))
            rows = ["timestamp,gpu,test,result"]
            for slot in ("GPU0", "GPU1", "GPU2", "GPU3", "GPU4", "GPU5", "GPU6", "GPU7"):
                for test in ("software", "pcie", "memory", "memory_bandwidth", "diagnostic", "targeted_stress",
                             "targeted_power", "nvbandwidth", "nccl_tests"):
                    bad = (h in sdc_hosts and slot == "GPU3" and test == "diagnostic") or \
                          (h in diagfail_hosts and slot == "GPU5" and test == "memory")
                    rows.append(f"{ts_d},{where[slot]},{test},{'fail' if bad else 'pass'}")
            (out / h).mkdir(exist_ok=True)
            (out / h / "diag.csv").write_text("\n".join(rows) + "\n")
        if h in iommu_hosts:
            k.line(t0 + 7000, "DMAR: DRHD: handling fault status reg 2")
            k.line(t0 + 7000, "DMAR: [DMA Write NO_PASID] Request device [9a:00.0] fault addr 0x7f2a1000 [fault reason 0x05] PTE Write access is not set")
        if h in storm_hosts:  # a failing riser floods the log
            for j in range(1500):
                k.aer_ce(t0 + 8000 + j * 0.02, where["GPU5"])
        if h in init_hosts:
            k.line(t0 + 30, f"NVRM: GPU {where['GPU1']}: RmInitAdapter failed! (0x62:0x55:2477)")
        if h in fm_hosts:
            k.line(t0 + 4000, "nvidia-fabricmanager.service: Main process exited, code=exited, status=1/FAILURE")
        if h in orphan_hosts:  # cleanup with no earlier error: the root cause line was lost
            k.xid(t0 + 5000, where["GPU0"], 45, "Ch 00000010")
        if h in lemon_hosts:  # cleanup right after a real error: not an orphan
            k.xid(tl + 10, where["GPU2"], 45, "Ch 00000008")
        if h in gsp_hosts:
            for j in range(2):
                k.xid(t0 + 6000 + j * 3600, where["GPU4"], 119, "Timeout after 6s of waiting for RPC response from GPU0 GSP!")
        if telemetry and h in crc_hosts + replay_hosts + spare_hosts + power_hosts + control_hosts:
            rows = ["timestamp,gpu,field,value"]
            names = {"crc": krng.choice(["DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL", "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_TOTAL"]),
                     "replay": krng.choice(["DCGM_FI_DEV_PCIE_REPLAY_COUNTER", "DCGM_FI_DEV_PCIE_REPLAY_TOTAL"])}
            for step in range(13):  # one sample every 5 minutes for an hour
                ts_s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + 7200 + step * 300))
                for slot in ("GPU0", "GPU6"):
                    bdf = where[slot]
                    crc = step * (5 * 300 if (h in crc_hosts and slot == "GPU6") else 2)          # 300/min vs ~0
                    rep = step * (5 * 50 if (h in replay_hosts and slot == "GPU0") else 1)        # 50/min vs ~0
                    pwr = step * int((0.6 if h in power_hosts else 0.01) * 300e9)                  # 60 % vs 1 % of time
                    low = 1 if (h in spare_hosts and slot == "GPU6" and step >= 6) else 0
                    rows += [f"{ts_s},{bdf},{names['crc']},{crc}", f"{ts_s},{bdf},{names['replay']},{rep}",
                             f"{ts_s},{bdf},DCGM_FI_DEV_POWER_VIOLATION,{pwr}",
                             f"{ts_s},{bdf},DCGM_FI_DEV_BANK_REMAP_AVAIL_LOW,{low}"]
            (out / h).mkdir(exist_ok=True)
            (out / h / "dcgm.csv").write_text("\n".join(rows) + "\n")
        if h in esc_hosts:  # repeated contained ECC errors on one GPU; the last host has only two
            n = 5 if h in esc_hosts[:escalate] else 2
            for j in range(n):
                k.xid(t0 + 2000 + j * 1800, where["GPU3"], 94, "Contained: SM (0x1). RST: No, D-RST: No")
        if h in unk_hosts:
            k.xid(t0 + 900, where["GPU1"], 154, "GPU recovery action changed from 0x0 (None) to 0x2 (Node Reboot Required).")
        d = out / h
        d.mkdir(exist_ok=True)
        (d / "sel.log").write_text("\n".join(w.lines) + "\n")
        (d / "pcie.txt").write_text("\n".join(tree) + "\n")
        if kernel:
            (d / "kernel.log").write_text(k.text())
        inv_rows.append({"host": h, "fw": "3.2.1" if broken else "3.1.9",
                         "bios": rng.choice(["2.18", "2.19"]), "rack": f"R{rng.randint(1, 40):02d}"})
    with open(out / "inventory.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["host", "fw", "bios", "rack"])
        wr.writeheader()
        wr.writerows(inv_rows)
    return truth
