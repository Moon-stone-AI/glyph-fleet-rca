"""Levels 1 and 2: raw log line -> word glyphs -> concept glyph + semantic arguments.

Three roads, one glyph:
  * binary 16-byte IPMI SEL records are decoded by (sensor type, sensor number, offset);
  * text SEL lines in any vendor dialect go through the lexicon (L1) and phrase rules (L2);
  * Linux kernel lines (Xid, SXid, MCE, EDAC, AER, panic, ...) go through ordered kernel rules.

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CODEBOOK_DIR = Path(__file__).resolve().parent.parent / "codebook"
DEFAULT_CODEBOOK = CODEBOOK_DIR / "sel_v0.1.yaml"
DEFAULT_CODEBOOKS = (CODEBOOK_DIR / "sel_v0.1.yaml", CODEBOOK_DIR / "kernel_v0.2.yaml",
                     CODEBOOK_DIR / "telemetry_v0.2.yaml", CODEBOOK_DIR / "diag_v0.2.yaml",
                     CODEBOOK_DIR / "vendor_common_v0.3.yaml", CODEBOOK_DIR / "vendor_dell_v0.3.yaml",
                     CODEBOOK_DIR / "vendor_lenovo_v0.3.yaml")


@dataclass(frozen=True)
class Event:
    ts: float | None          # epoch seconds, None if the line carries no time
    concept: str              # L2 concept glyph, or UNKNOWN
    args: tuple[str, ...]     # semantic arguments, e.g. ("bdf=0000:41:00.0",) or ("dimm=A2",)
    words: tuple[str, ...]    # L1 word glyphs (empty for binary records)
    raw: str                  # the original line, kept so a human can read the last words
    source: str = "sel"       # sel | kernel | telemetry | diag


@dataclass
class Codebook:
    version: str
    concepts: dict
    binary: list
    lexicon: dict
    stop_words: set
    phrases: list = field(default_factory=list)
    xid: dict = field(default_factory=dict)
    kernel: list = field(default_factory=list)
    escalation: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)   # metric name (exporter or DCGM field) -> rule
    aer_storm: dict = field(default_factory=dict)
    diag: dict = field(default_factory=dict)
    message_ids: dict = field(default_factory=dict)  # vendor message ID (e.g. Dell MEM0001) -> rule

    @classmethod
    def load(cls, paths=DEFAULT_CODEBOOKS) -> "Codebook":
        """Loads one codebook file or several; later files add concepts and roads."""
        if isinstance(paths, (str, Path)):
            paths = (paths,)
        cb = cls("", {}, [], {}, set(), [])
        versions = []
        for path in paths:
            d = yaml.safe_load(Path(path).read_text())
            versions.append(str(d.get("version", "")))
            cb.concepts.update(d.get("concepts", {}))
            cb.binary += d.get("sel_binary", [])
            cb.lexicon.update({w: g for g, words in d.get("lexicon", {}).items() for w in words})
            cb.stop_words |= set(d.get("stop_words", []))
            cb.phrases += [(p["concept"], [frozenset(a) for a in p["any_of"]]) for p in d.get("phrases", [])]
            cb.xid.update({int(k): v for k, v in d.get("xid", {}).items()})
            cb.kernel += [(r["name"], re.compile(r["re"]), r["concept"]) for r in d.get("kernel", [])]
            cb.escalation += d.get("escalation", [])
            cb.aer_storm.update(d.get("aer_storm", {}))
            cb.diag.update(d.get("diag", {}))
            cb.message_ids.update(d.get("message_ids", {}))
            for m in d.get("metrics", []):
                for n in m["names"]:
                    cb.metrics[n] = m
        cb.version = "+".join(versions)
        return cb

    def severity(self, concept: str) -> str:
        return self.concepts.get(concept, {}).get("severity", "unknown")

    def triage(self, concept: str) -> str:
        return self.concepts.get(concept, {}).get("triage", "observe")

    def cause(self, concept: str) -> str:
        return self.concepts.get(concept, {}).get("cause", "unknown")

    def fatal(self, concept: str) -> bool:
        """Concepts that end a node's life (CATERR, kernel panic, thermal trip, ...)."""
        return bool(self.concepts.get(concept, {}).get("fatal", False))


def readable(e: Event) -> str:
    """The raw line, plus its decoded meaning when the raw line is a binary record."""
    if HEX_RECORD.match(e.raw):
        return f"{e.raw}  [{e.concept}{' ' + ' '.join(e.args) if e.args else ''}]"
    return e.raw


# ---------------------------------------------------------------- shared patterns
HEX_RECORD = re.compile(r"^\s*([0-9a-fA-F]{2}\s?){16}\s*$")
TS_US = re.compile(r"(\d{2})/(\d{2})/(\d{4}) \| (\d{2}):(\d{2}):(\d{2})")   # ipmitool: MM/DD/YYYY | hh:mm:ss
TS_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")
TS_DMESG = re.compile(r"\[\w{3} (\w{3}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\d{4})\]")  # dmesg -T
MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
# variable fields that never carry meaning: record ids, times, hex values, numbers
VARIABLE = re.compile(
    r"^\s*[0-9a-f]+(?= \|)|\d{2}/\d{2}/\d{4} \| \d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"|[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]|0x[0-9a-f]+|\b[a-f]?\d+\b|\[\d+\]",
    re.I,
)
SEM_ARG = re.compile(r"\b(slot|port|cpu|processor|dimm|module)\s+([a-f]?\d+)", re.I)
BDF = re.compile(r"\b([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])\b", re.I)
WORD = re.compile(r"[a-z]+(?:-[a-z]+)*")
EDAC_LOC = re.compile(r"(CPU_SrcID#\d+_MC#\d+_Chan#\d+_DIMM#\d+)")
CPU_ARG = re.compile(r"\bCPU (\d+)\b")
MSG_ID = re.compile(r"\b(FQX[A-Z]{2,6}\d{4}[A-Z]|[A-Z]{2,4}\d{2,4})\b")  # vendor IDs: Dell MEM0001, PDR16; Lenovo FQXSPIO0013N
BUS_DEV_FN = re.compile(r"\bbus (\d+) device (\d+) function (\d+)", re.I)   # Dell text form
HEX_BDF = re.compile(r"\bBus:?\s*0x([0-9a-f]{1,2})\s+Dev(?:ice)?:?\s*0x([0-9a-f]{1,2})\s+F(?:unc(?:tion)?)?:?\s*0x([0-7])", re.I)  # explicit hex (Dell-style, Lenovo UEFI)
GPU_IDX = re.compile(r"\bGPU[_ ](\d+)\b|\bGPU\)? device (\d+)\b", re.I)                   # iDRAC: GPU_3, GPU device 2
ARG_NAME = {"processor": "cpu", "module": "dimm"}


def parse_ts(line: str) -> float | None:
    m = TS_US.search(line)
    if m:
        mo, d, y, hh, mm, ss = map(int, m.groups())
        return float(calendar.timegm((y, mo, d, hh, mm, ss, 0, 0, 0)))
    m = TS_ISO.search(line)
    if m:
        y, mo, d, hh, mm, ss = map(int, m.groups())
        return float(calendar.timegm((y, mo, d, hh, mm, ss, 0, 0, 0)))
    m = TS_DMESG.search(line)
    if m and m.group(1) in MONTHS:
        mon, d, hh, mm, ss, y = m.groups()
        return float(calendar.timegm((int(y), MONTHS[mon], int(d), int(hh), int(mm), int(ss), 0, 0, 0)))
    return None


def text_args(line: str) -> tuple[str, ...]:
    args = [f"{ARG_NAME.get(k.lower(), k.lower())}={v.upper()}" for k, v in SEM_ARG.findall(line)]
    args += [f"bdf={b.lower()}" for b in BDF.findall(line)]
    # Dell prints "bus 2 device 0 function 0"; kept as written: whether the numbers are decimal is not confirmed
    args += [f"pci=bus{b}.dev{d}.fn{f}" for b, d, f in BUS_DEV_FN.findall(line)]
    # only an explicit 0x prefix is trusted to be hexadecimal, so only this form becomes a bdf
    args += [f"bdf=0000:{int(b, 16):02x}:{int(d, 16):02x}.{f}" for b, d, f in HEX_BDF.findall(line)]
    args += [f"gpu={a or b}" for a, b in GPU_IDX.findall(line)]
    return tuple(args)


# ---------------------------------------------------------------- binary decoders (Intel SEL guide)
def _cpu_bitmap(ed2, ed3):
    cpus = [f"cpu={i + 1}" for i in range(4) if ed3 >> i & 1]
    return tuple(cpus) if len(cpus) == 1 else ("cpu=ambiguous",)  # Table 61: >1 bit = source unknown


def _mem_location(ed2, ed3):  # Table 75: ED2 [7:4] DIMM, [3:0] rank; ED3 [7:4] socket, [3:0] channel
    return (f"cpu={(ed3 >> 4) + 1}", f"dimm={'ABCDEFGH'[ed3 & 0xF]}{(ed2 >> 4) + 1}")


def _pcie_bdf(ed2, ed3):  # Tables 83, 85: ED2 bus; ED3 [7:3] device, [2:0] function
    return (f"bdf=0000:{ed2:02x}:{ed3 >> 3:02x}.{ed3 & 7}",)


DECODERS = {"cpu_bitmap": _cpu_bitmap, "mem_location": _mem_location, "pcie_bdf": _pcie_bdf,
            "none": lambda a, b: ()}


class Encoder:
    """Turns raw lines into Events. Caches by line template, so a storm of identical
    events is understood once and counted many times."""

    def __init__(self, codebook: Codebook | None = None):
        self.cb = codebook or Codebook.load()
        self._cache: dict[str, tuple[tuple[str, ...], str]] = {}

    # ---- road 1
    def decode_binary(self, line: str) -> Event:
        b = bytes.fromhex(line.replace(" ", ""))
        ts = float(int.from_bytes(b[3:7], "little"))
        st, sn, ed1, ed2, ed3 = b[10], b[11], b[13], b[14], b[15]
        off = ed1 & 0x0F
        for m in self.cb.binary:
            if m["st"] == st and m["sn"] in (None, sn) and m["offset"] in (None, off):
                return Event(ts, m["concept"], DECODERS[m["args"]](ed2, ed3), (), line.strip())
        return Event(ts, "UNKNOWN", (f"st={st:02x}", f"sn={sn:02x}", f"off={off:x}"), (), line.strip())

    # ---- road 2
    def words(self, line: str) -> tuple[str, ...]:
        out: list[str] = []
        for w in WORD.findall(VARIABLE.sub(" ", line.lower())):
            if w in self.cb.stop_words:
                continue
            g = self.cb.lexicon.get(w, "?" + w)
            if g == "META":
                continue
            if not out or out[-1] != g:
                out.append(g)
        return tuple(out)

    def concept(self, words: tuple[str, ...]) -> str:
        s = set(words)
        for concept, alternatives in self.cb.phrases:
            if any(a <= s for a in alternatives):
                return concept
        return "UNKNOWN"

    # ---- road 3
    def encode_kernel(self, line: str) -> Event | None:
        """A kernel line -> Event, or None for routine kernel chatter."""
        for name, rx, concept in self.cb.kernel:
            m = rx.search(line)
            if not m:
                continue
            g = m.groupdict()
            args = []
            if g.get("gpu"):
                b = g["gpu"].lower()
                args.append(f"bdf={b if '.' in b else b + '.0'}")
            if g.get("xid"):
                args.append(f"xid={int(g['xid'])}")
                concept = self.cb.xid.get(int(g["xid"]), "GPU.XID.OTHER")
            if g.get("sxid"):
                args.append(f"sxid={int(g['sxid'])}")
            if not g.get("gpu"):
                b = BDF.search(line)
                if b:
                    args.append(f"bdf={b.group(1).lower()}")
            loc = EDAC_LOC.search(line)
            if loc:
                args.append(f"dimm={loc.group(1)}")
            c = CPU_ARG.search(line)
            if c and name == "mce":
                args.append(f"cpu={c.group(1)}")
            return Event(parse_ts(line), concept, tuple(args), (), line.rstrip("\n"), "kernel")
        return None

    # ---- road 4
    def encode_telemetry_file(self, path: str | Path) -> list[Event]:
        """dcgm.csv (timestamp,gpu,field,value) -> events where a rule's threshold is crossed.
        Counters become rates per minute between consecutive samples of the same GPU and metric;
        gauges fire when they turn on (not on every sample, so a stuck gauge is one event)."""
        import csv
        rows = []
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                rule = self.cb.metrics.get(row.get("field", ""))
                ts = parse_ts(row.get("timestamp", ""))
                if rule is None or ts is None:
                    continue
                rows.append((ts, row["gpu"].lower(), rule, float(row["value"]), row))
        rows.sort(key=lambda r: r[0])
        last: dict = {}
        out = []
        for ts, gpu, rule, value, row in rows:
            key = (gpu, rule["glyph"], rule["kind"])
            prev = last.get(key)
            last[key] = (ts, value)
            line = f"{row['timestamp']},{row['gpu']},{row['field']},{row['value']}"
            if rule["kind"] == "counter":
                if prev is None or ts <= prev[0] or value < prev[1]:
                    continue  # first sample, or a counter reset
                rate = (value - prev[1]) / ((ts - prev[0]) / 60.0)
                if rate > float(rule["rate_per_min_above"]):
                    out.append(Event(ts, rule["glyph"], (f"bdf={gpu}",), (), f"{line}  [{rate:,.0f}/min]", "telemetry"))
            else:
                was_on = prev is not None and prev[1] > float(rule["value_above"])
                if value > float(rule["value_above"]) and not was_on:
                    out.append(Event(ts, rule["glyph"], (f"bdf={gpu}",), (), line, "telemetry"))
        return out

    # ---- road 5
    def encode_diag_file(self, path: str | Path) -> list[Event]:
        """diag.csv (timestamp,gpu,test,result) -> an event for every failed test."""
        import csv
        math = {t.lower() for t in self.cb.diag.get("math_tests", [])}
        out = []
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("result", "").strip().lower() != "fail":
                    continue
                test = row.get("test", "").strip().lower()
                concept = "GPU.SDC" if test in math else "GPU.DIAG.FAIL"
                line = f"{row['timestamp']},{row['gpu']},{row['test']},{row['result']}"
                out.append(Event(parse_ts(row.get("timestamp", "")), concept,
                                 (f"bdf={row['gpu'].lower()}", f"test={test}"), (), line, "diag"))
        return out

    def encode(self, line: str) -> Event:
        if HEX_RECORD.match(line):
            return self.decode_binary(line)
        for mid in MSG_ID.findall(line):
            rule = self.cb.message_ids.get(mid)
            if rule:
                return Event(parse_ts(line), rule["concept"], (f"msg={mid}",) + text_args(line), (), line.rstrip("\n"))
        key = VARIABLE.sub("#", line)
        hit = self._cache.get(key)
        if hit is None:
            w = self.words(line)
            hit = self._cache[key] = (w, self.concept(w))
        return Event(parse_ts(line), hit[1], text_args(line), hit[0], line.rstrip("\n"))

    def encode_file(self, path: str | Path, source: str = "sel") -> list[Event]:
        if source == "telemetry":
            return self.encode_telemetry_file(path)
        if source == "diag":
            return self.encode_diag_file(path)
        with open(path, errors="replace") as f:
            if source == "kernel":
                return [e for e in (self.encode_kernel(l) for l in f if l.strip()) if e is not None]
            return [self.encode(l) for l in f if l.strip()]
