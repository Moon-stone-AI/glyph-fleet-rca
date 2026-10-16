"""Levels 3 and 4: runs with counters per server, signatures across the fleet.

L3  consecutive identical concepts -> one run: concept x count, first/last time, argument histogram.
L4  servers with the same run sequence -> one fleet signature x number of servers.
Rare signatures are the 'survivors': the few servers that recorded something the storm did not.

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .encode import Codebook, Event, readable

SILENCE = "SILENCE"


@dataclass
class Run:
    concept: str
    count: int
    first: float | None
    last: float | None
    args: Counter = field(default_factory=Counter)
    sample: str = ""  # one raw line kept verbatim (the first of the run)

    @property
    def rate_per_s(self) -> float | None:
        if self.first is None or self.last is None or self.last <= self.first:
            return None
        return self.count / (self.last - self.first)


def to_runs(events: list[Event]) -> list[Run]:
    runs: list[Run] = []
    for e in events:
        if runs and runs[-1].concept == e.concept:
            r = runs[-1]
            r.count += 1
            r.last = e.ts if e.ts is not None else r.last
            r.args.update(e.args)
        else:
            runs.append(Run(e.concept, 1, e.ts, e.ts, Counter(e.args), e.raw))
    last_t = runs[-1].last if runs else None
    runs.append(Run(SILENCE, 1, last_t, None))  # the log ends: silence is an event too
    return runs


def signature(runs: list[Run], cb: Codebook) -> tuple[str, ...]:
    """Ordered concepts that matter: info-level events (boot, temperature normal) are dropped."""
    return tuple(r.concept for r in runs if cb.severity(r.concept) != "info")


@dataclass
class FleetView:
    runs: dict[str, list[Run]]
    groups: dict[tuple[str, ...], list[str]]
    last_words: dict[str, str]

    def ranked(self):
        return sorted(self.groups.items(), key=lambda kv: -len(kv[1]))

    def rare(self, max_size: int | None = None) -> list[tuple[tuple[str, ...], list[str]]]:
        """Signatures shared by very few servers, next to a big group (a storm)."""
        n = sum(len(h) for h in self.groups.values())
        limit = max_size if max_size is not None else max(3, n // 1000)
        biggest = max(len(h) for h in self.groups.values())
        return [(s, h) for s, h in self.groups.items() if len(h) <= limit and biggest > 10 * len(h)]

    def survivors(self, storm_min: int = 50):
        """Rare signatures that contain a storm's whole story and then say something more:
        the servers that lived a moment longer than the storm and recorded why."""
        storms = [s[:-1] for s, h in self.groups.items() if len(h) >= storm_min and len(s) > 1]
        out = []
        for s, hosts in self.rare():
            body = s[:-1]
            for st in storms:
                it = iter(body)
                if len(body) > len(st) and all(c in it for c in st):  # storm is a subsequence of body
                    out.append((s, hosts))
                    break
        return out


def build_fleet(events_by_host: dict[str, list[Event]], cb: Codebook) -> FleetView:
    runs = {h: to_runs(ev) for h, ev in events_by_host.items()}
    groups: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for h, rs in runs.items():
        groups[signature(rs, cb)].append(h)
    last_words = {h: (readable(ev[-1]) if ev else "") for h, ev in events_by_host.items()}
    return FleetView(runs, dict(groups), last_words)


def crashed(fv: FleetView, cb: Codebook) -> set[str]:
    """Servers that died: they recorded a fatal concept (CATERR, panic, thermal trip, ...)."""
    return {h for h, rs in fv.runs.items() if any(cb.fatal(r.concept) for r in rs)}


def simultaneity(fv: FleetView, cb: Codebook, hosts: set[str]) -> float | None:
    """Spread (seconds) of the last critical event across the crashed cohort.
    A small spread over thousands of servers points to a common cause."""
    times = []
    for h in hosts:
        crit = [r.last for r in fv.runs[h] if cb.fatal(r.concept) and r.last is not None]
        if crit:
            times.append(min(crit))
    if len(times) < 2:
        return None
    times.sort()
    lo, hi = times[int(0.05 * len(times))], times[int(0.95 * len(times)) - 1]
    return hi - lo


def load_inventory(path: str) -> dict[str, dict[str, str]]:
    with open(path, newline="") as f:
        return {row["host"]: {k: v for k, v in row.items() if k != "host"} for row in csv.DictReader(f)}


def inventory_join(inv: dict[str, dict[str, str]], cohort: set[str], min_rate=0.9, max_other=0.1):
    """For every inventory attribute, find a value that (almost) all crashed servers share and
    (almost) no healthy server has. Returns [(attribute, value, crash_rate, other_rate, n)]."""
    findings = []
    attrs = {k for row in inv.values() for k in row}
    for a in sorted(attrs):
        total, bad = Counter(), Counter()
        for h, row in inv.items():
            v = row.get(a, "")
            total[v] += 1
            bad[v] += h in cohort
        for v, n in total.items():
            rate = bad[v] / n
            others_bad = sum(bad[x] for x in total if x != v)
            others_n = sum(total[x] for x in total if x != v)
            other_rate = others_bad / others_n if others_n else 0.0
            covers = bad[v] / max(1, len(cohort))
            if rate >= min_rate and other_rate <= max_other and covers >= min_rate:
                findings.append((a, v, rate, other_rate, n))
    return findings
