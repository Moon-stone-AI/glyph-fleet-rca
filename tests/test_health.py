"""v0.2.1 tests: GPU health index. Run: python -m unittest discover -s tests -v

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glyphrca.health import band, load_config  # noqa: E402
from glyphrca.report import analyse_fleet  # noqa: E402
from glyphrca.synth import generate  # noqa: E402


class GpuHealth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.truth = generate(cls.dir, bad=300, ok=150, flood=(10, 20), seed=13)
        cls.r = analyse_fleet(cls.dir)
        cls.cards = cls.r["gpu_health"]
        cls.by_host = {}
        for c in cls.cards:
            cls.by_host.setdefault(c["host"], []).append(c)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def worst(self, host):
        return min(self.by_host[host], key=lambda c: c["score"])

    def test_every_gpu_has_a_card(self):
        self.assertEqual(len(self.cards), 450 * 8)
        self.assertTrue(all(0 <= c["score"] <= 100 for c in self.cards))

    def test_hard_caps(self):
        for h in self.truth["sdc"] + self.truth["remap_fail"]:
            self.assertLessEqual(self.worst(h)["score"], 10)
            self.assertEqual(self.worst(h)["band"], "do not sell")
        for h in self.truth["no_spare"]:
            self.assertLessEqual(self.worst(h)["score"], 30)

    def test_common_mode_storm_is_not_charged_to_the_gpus(self):
        broken = [f"node-{i:05d}" for i in range(300)]
        for h in broken:
            for c in self.by_host[h]:
                self.assertNotIn("links", c["breakdown"], (h, c["bdf"]))

    def test_known_deductions(self):
        for h in self.truth["escalate"]:  # 5 x Xid 94 -> 5 x 3 points, rule ceiling 15
            self.assertEqual(self.worst(h)["breakdown"]["memory"], 15)
        for h in self.truth["degraded"]:  # GPU1 trained at x8
            self.assertEqual(self.worst(h)["breakdown"]["links"], 10)
        for h in self.truth["gsp_repeat"]:  # 2 x Xid 119
            self.assertEqual(self.worst(h)["breakdown"]["severe_xid"], 16)

    def test_uncorrectable_memory_caps(self):
        for h in self.truth["lemons"]:  # one Xid 48 on GPU2
            c = [c for c in self.by_host[h] if c["evidence"].get("GPU.MEM.ECC.DBE")]
            self.assertTrue(c)
            self.assertLessEqual(c[0]["score"], 74)
            self.assertEqual(c[0]["band"], "caution")
        from glyphrca.health import gpu_health
        from glyphrca.encode import Event
        gpus = {("h", "0000:41:00.0"): {"host": "h", "bdf": "0000:41:00.0", "hours": ""}}
        ev = lambda c, t: Event(t, c, ("bdf=0000:41:00.0",), (), "")  # noqa: E731
        one = gpu_health({"h": [ev("GPU.MEM.ECC.DBE", 1), ev("GPU.MEM.REMAP.EVENT", 2)]}, {}, gpus, set())[0]
        self.assertEqual((one["score"], one["band"]), (74, "caution"))  # 48 + its remap = one incident
        two = gpu_health({"h": [ev("GPU.MEM.ECC.DBE", 1), ev("GPU.MEM.ECC.DBE", 9)]}, {}, gpus, set())[0]
        self.assertEqual(two["band"], "do not sell")
        self.assertLessEqual(two["score"], 49)

    def test_quiet_gpu_loses_only_age(self):
        quiet = [c for c in self.cards if set(c["breakdown"]) <= {"age"} and not c["verdicts"]]
        self.assertGreater(len(quiet), 3000)
        for c in quiet:
            self.assertAlmostEqual(c["score"], 100 - c["hours"] / 8760, delta=0.15)  # rounding to 0.1

    def test_fleet_stats_by_model(self):
        st = self.r["gpu_health_by_model"]
        self.assertEqual(set(st), {"H100", "H200", "A100"})
        self.assertEqual(sum(s["gpus"] for s in st.values()), 3600)

    def test_bands(self):
        cfg = load_config()
        self.assertEqual([band(x, cfg) for x in (95, 80, 60, 20)], ["excellent", "good", "caution", "do not sell"])


if __name__ == "__main__":
    unittest.main()
