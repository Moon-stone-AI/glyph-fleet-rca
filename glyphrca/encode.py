"""Levels 1 and 2: raw SEL line -> word glyphs -> concept glyph + semantic arguments.

Two roads, one glyph:
  * binary 16-byte IPMI SEL records are decoded by (sensor type, sensor number, offset);
  * text lines in any vendor dialect go through the lexicon (L1) and phrase rules (L2).

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CODEBOOK = Path(__file__).resolve().parent.parent / "codebook" / "sel_v0.1.yaml"


@dataclass(frozen=True)
class Event:
    ts: float | None          # epoch seconds, None if the line carries no time
    concept: str              # L2 concept glyph, or UNKNOWN
    args: tuple[str, ...]     # semantic arguments, e.g. ("bdf=0000:41:00.0",) or ("dimm=A2",)
    words: tuple[str, ...]    # L1 word glyphs (empty for binary records)
    raw: str                  # the original line, kept so a human can read the last words


@dataclass
class Codebook:
    version: str
    concepts: dict
    binary: list
    lexicon: dict
    stop_words: set
    phrases: list = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CODEBOOK) -> "Codebook":
        d = yaml.safe_load(Path(path).read_text())
        lex = {w: g for g, words in d["lexicon"].items() for w in words}
        phrases = [(p["concept"], [frozenset(a) for a in p["any_of"]]) for p in d["phrases"]]
        return cls(d["version"], d["concepts"], d["sel_binary"], lex, set(d["stop_words"]), phrases)

    def severity(self, concept: str) -> str:
        return self.concepts.get(concept, {}).get("severity", "unknown")

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
# variable fields that never carry meaning: record ids, times, hex values, numbers
VARIABLE = re.compile(
    r"^\s*[0-9a-f]+(?= \|)|\d{2}/\d{2}/\d{4} \| \d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"|[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]|0x[0-9a-f]+|\b[a-f]?\d+\b|\[\d+\]",
    re.I,
)
SEM_ARG = re.compile(r"\b(slot|port|cpu|processor|dimm|module)\s+([a-f]?\d+)", re.I)
BDF = re.compile(r"\b([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])\b", re.I)
WORD = re.compile(r"[a-z]+(?:-[a-z]+)*")
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
    return None


def text_args(line: str) -> tuple[str, ...]:
    args = [f"{ARG_NAME.get(k.lower(), k.lower())}={v.upper()}" for k, v in SEM_ARG.findall(line)]
    args += [f"bdf={b.lower()}" for b in BDF.findall(line)]
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

    def encode(self, line: str) -> Event:
        if HEX_RECORD.match(line):
            return self.decode_binary(line)
        key = VARIABLE.sub("#", line)
        hit = self._cache.get(key)
        if hit is None:
            w = self.words(line)
            hit = self._cache[key] = (w, self.concept(w))
        return Event(parse_ts(line), hit[1], text_args(line), hit[0], line.rstrip("\n"))

    def encode_file(self, path: str | Path) -> list[Event]:
        with open(path, errors="replace") as f:
            return [self.encode(l) for l in f if l.strip()]
