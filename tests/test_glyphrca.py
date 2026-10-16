"""Tests. Run: python -m unittest discover -s tests -v

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
import json
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glyphrca.encode import Codebook, Encoder  # noqa: E402
from glyphrca.fleet import to_runs  # noqa: E402
from glyphrca.report import analyse_fleet  # noqa: E402
from glyphrca.synth import Writer, generate  # noqa: E402
from glyphrca.tier2 import public_report  # noqa: E402

ENC = Encoder(Codebook.load())
DIALECTS = ["ipmitool", "vendorD", "vendorH", "hex"]


def one_line(dialect, method, *args):
    w = Writer(dialect, random.Random(1))
    getattr(w, method)(1_791_900_000, *args)
    return w.lines[-1]


class Level1And2(unittest.TestCase):
    def test_four_dialects_one_glyph(self):
        cases = [("mem_corr", (), "MEM.ECC.CORR"), ("caterr", (), "CPU.IERR.CATERR"),
                 ("panic", (), "OS.KERNEL.PANIC"), ("pcie_corr", ("0000:43:00.0",), "PCIE.CORR"),
                 ("pcie_link_down", ("0000:43:00.0",), "PCIE.LINK.DOWN")]
        for method, args, expected in cases:
            for d in DIALECTS:
                with self.subTest(method=method, dialect=d):
                    self.assertEqual(ENC.encode(one_line(d, method, *args)).concept, expected)

    def test_binary_record_arguments(self):
        # Intel guide Table 75: ED2[7:4] DIMM index, ED3[7:4] socket, ED3[3:0] channel
        e = ENC.encode("01000256341200330004" "0c026fa0" "1012")
        self.assertEqual(e.concept, "MEM.ECC.CORR")
        self.assertEqual(e.args, ("cpu=2", "dimm=C2"))
        # Table 85: ED2 bus, ED3 [7:3] device [2:0] function
        e = ENC.encode("0100025634120033000413" "0571a0" "4309")
        self.assertEqual(e.concept, "PCIE.CORR")
        self.assertEqual(e.args, ("bdf=0000:43:01.1",))

    def test_text_bdf_argument_survives(self):
        e = ENC.encode(one_line("vendorD", "pcie_corr", "0000:43:00.0"))
        self.assertIn("bdf=0000:43:00.0", e.args)

    def test_non_critical_is_not_critical(self):
        line = "  1a | 10/13/2026 | 08:00:00 | Temperature #0x31 | Upper Non-critical going high | Deasserted"
        self.assertEqual(ENC.encode(line).concept, "THERMAL.OK")

    def test_unknown_word_is_a_data_gap(self):
        e = ENC.encode("10/13/2026 | 08:00:00 | Quantum flux capacitor misaligned")
        self.assertEqual(e.concept, "UNKNOWN")
        self.assertIn("?quantum", e.words)


class Level3(unittest.TestCase):
    def test_storm_collapses_to_one_run_with_counter_and_rate(self):
        lines = [one_line("ipmitool", "mem_corr") for _ in range(500)]
        events = [ENC.encode(l) for l in lines]
        runs = to_runs(events)
        self.assertEqual([r.concept for r in runs], ["MEM.ECC.CORR", "SILENCE"])
        self.assertEqual(runs[0].count, 500)


class Survivors(unittest.TestCase):
    def test_survivor_is_storm_plus_more_not_any_rare_signature(self):
        from glyphrca.encode import Event
        from glyphrca.fleet import build_fleet
        cb = Codebook.load()
        storm = [Event(1.0, "MEM.ECC.CORR", (), (), "m"), Event(2.0, "CPU.IERR.CATERR", (), (), "c")]
        hosts = {f"h{i}": list(storm) for i in range(2000)}
        hosts["survivor"] = storm + [Event(3.0, "OS.KERNEL.PANIC", (), (), "panic")]
        hosts["other"] = [Event(1.0, "PSU.FAIL", (), (), "psu")]
        fv = build_fleet(hosts, cb)
        self.assertEqual([h for _, hs in fv.survivors() for h in hs], ["survivor"])
        self.assertEqual(sorted(h for _, hs in fv.rare() for h in hs), ["other", "survivor"])


class FleetEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.truth = generate(cls.dir, bad=300, ok=150, flood=(10, 20), seed=7)
        cls.r = analyse_fleet(cls.dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def test_survivor_found(self):
        self.assertEqual([s["host"] for s in self.r["survivors"]], [self.truth["survivor"]])
        self.assertIn("OS.KERNEL.PANIC", self.r["survivors"][0]["signature"])

    def test_common_mode_and_firmware_verdict(self):
        self.assertTrue(self.r["common_mode"])
        self.assertLess(self.r["crash_spread_s"], 5)
        self.assertEqual([(f["attribute"], f["value"]) for f in self.r["inventory"]], [("fw", "3.2.1")])

    def test_pcie_tree_isolates_switch_gpu_missing_and_degraded(self):
        p = self.r["pcie"]
        self.assertEqual(set(p["upstream_suspects"]), {self.truth["bad_switch"]})
        self.assertEqual(sorted(p["upstream_suspects"][self.truth["bad_switch"]]), sorted(self.truth["switch_fault"]))
        self.assertEqual(set(p["device_suspects"]), {self.truth["bad_gpu"]})
        self.assertEqual(sorted(p["missing"][self.truth["missing_slot"]]), sorted(self.truth["missing_gpu"]))
        self.assertEqual(sorted(p["degraded"][self.truth["degraded_slot"]]), sorted(self.truth["degraded"]))

    def test_compression_levels_monotonic(self):
        L = self.r["levels"]
        self.assertTrue(L["raw"] > L["L1"] > L["L2"] > L["L3"] > L["L4"])

    def test_no_data_gaps_in_synthetic_dialects(self):
        self.assertEqual(self.r["unknown_words"], {})


class Tier2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = Path(tempfile.mkdtemp())
        cls.truth = generate(d, bad=120, ok=60, flood=(5, 10), seed=3)
        cls.r = analyse_fleet(d)
        shutil.rmtree(d)

    def test_public_report_leaks_nothing(self):
        pub, priv = public_report(self.r, "k1")
        text = json.dumps(pub)
        for secret in ["CPU.IERR.CATERR", "OS.KERNEL.PANIC", "node-", "3.2.1", "RP1/SW1", "GPU", "fw"]:
            self.assertNotIn(secret, text, secret)
        self.assertIn("SILENCE", text)
        survivor_token = pub["survivors"][0]["host"]
        self.assertEqual(priv[survivor_token], self.truth["survivor"])

    def test_tokens_stable_per_key(self):
        a, _ = public_report(self.r, "k1")
        b, _ = public_report(self.r, "k1")
        c, _ = public_report(self.r, "k2")
        self.assertEqual(a, b)
        self.assertNotEqual(a["survivors"], c["survivors"])


if __name__ == "__main__":
    unittest.main()
