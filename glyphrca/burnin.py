"""Burn-in summary: turns nvidia-smi CSV samples and a kernel log into one health record per card.

Input is the output of:
    nvidia-smi --query-gpu=timestamp,index,uuid,power.draw,power.limit,pcie.link.width.current,
        pcie.link.width.max,pcie.link.gen.current,pcie.link.gen.max,clocks.sm,temperature.gpu,
        ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total,
        clocks_throttle_reasons.active --format=csv -l 1
Field names differ between driver versions: check with `nvidia-smi --help-query-gpu` first.

No thresholds here. Records carry the numbers and a few objective flags (uncorrected ECC, degraded
PCIe link). Pass/fail limits for power, corrected ECC, clocks and temperature are set separately.
"""
import csv
import io
import statistics

NA = {"", "[N/A]", "N/A", "[Not Supported]"}


def _num(v):
    if v is None or v.strip() in NA:
        return None
    try:
        return float(v.strip())
    except ValueError:
        return None


def parse_smi_csv(text: str) -> dict[str, list[dict]]:
    """Group samples by GPU uuid. Column names have units stripped ('power.draw [W]' -> 'power.draw')."""
    reader = csv.reader(io.StringIO(text))
    header = [h.split(" [")[0].strip() for h in next(reader)]
    by_gpu: dict[str, list[dict]] = {}
    for row in reader:
        if not row:
            continue
        rec = dict(zip(header, [c.strip() for c in row]))
        by_gpu.setdefault(rec["uuid"], []).append(rec)
    return by_gpu


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _delta(samples, key):
    vals = [_num(s.get(key)) for s in samples]
    vals = [v for v in vals if v is not None]
    return (vals[-1] - vals[0]) if len(vals) >= 2 else None


def summarize(samples: list[dict], warmup_s: int = 300) -> dict:
    """One record for one card. The first `warmup_s` samples (1 Hz) are dropped from steady-state values."""
    steady = samples[warmup_s:] if len(samples) > warmup_s else samples
    width = [_num(s.get("pcie.link.width.current")) for s in steady]
    width_max = [_num(s.get("pcie.link.width.max")) for s in steady]
    gen = [_num(s.get("pcie.link.gen.current")) for s in steady]
    gen_max = [_num(s.get("pcie.link.gen.max")) for s in steady]
    throttle = sorted({s.get("clocks_throttle_reasons.active", "") for s in steady} - NA - {""})

    rec = {
        "gpu_uuid": samples[0]["uuid"],
        "samples": len(samples),
        "steady_samples": len(steady),
        "signals": {
            "power_draw_w_p50": _median([_num(s.get("power.draw")) for s in steady]),
            "power_limit_w": _median([_num(s.get("power.limit")) for s in steady]),
            "pcie_width_max_under_load": max((v for v in width if v is not None), default=None),
            "pcie_width_expected": _median(width_max),
            "pcie_gen_max_under_load": max((v for v in gen if v is not None), default=None),
            "pcie_gen_expected": _median(gen_max),
            "ecc_corrected_delta": _delta(samples, "ecc.errors.corrected.volatile.total"),
            "ecc_uncorrected_delta": _delta(samples, "ecc.errors.uncorrected.volatile.total"),
            "sm_clock_mhz_steady": _median([_num(s.get("clocks.sm")) for s in steady]),
            "temp_c_steady": _median([_num(s.get("temperature.gpu")) for s in steady]),
            "throttle_reasons": throttle,
        },
        "flags": [],
    }
    sig = rec["signals"]
    if sig["ecc_uncorrected_delta"] and sig["ecc_uncorrected_delta"] > 0:
        rec["flags"].append("ecc_uncorrected")
    if sig["pcie_width_expected"] and sig["pcie_width_max_under_load"] is not None \
            and sig["pcie_width_max_under_load"] < sig["pcie_width_expected"]:
        rec["flags"].append("pcie_width_degraded")
    if sig["pcie_gen_expected"] and sig["pcie_gen_max_under_load"] is not None \
            and sig["pcie_gen_max_under_load"] < sig["pcie_gen_expected"]:
        rec["flags"].append("pcie_gen_degraded")
    return rec


def count_xid(log_text: str) -> int:
    """Count NVRM Xid lines in a kernel log capture."""
    return sum(1 for line in log_text.splitlines() if "NVRM: Xid" in line)


def build_records(smi_csv: str, kernel_log: str = "", warmup_s: int = 300) -> list[dict]:
    """Records for every card in the capture. Xid count is per capture, not per card (the log has no uuid)."""
    recs = []
    xid = count_xid(kernel_log)
    for uuid, samples in parse_smi_csv(smi_csv).items():
        rec = summarize(samples, warmup_s)
        rec["signals"]["xid_count_capture"] = xid
        recs.append(rec)
    return recs
