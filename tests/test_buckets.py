"""v0.4 tests: symptom buckets (work queues). Run: python -m unittest discover -s tests -v

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glyphrca.report import analyse_fleet  # noqa: E402
from glyphrca.synth import generate  # noqa: E402


class Buckets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.truth = generate(cls.dir, bad=300, ok=150, flood=(10, 20), seed=13)
        cls.q = {q["bucket"]: q for q in analyse_fleet(cls.dir)["buckets"]}
        cls.where = {n["host"]: b for b, q in cls.q.items() for n in q["nodes"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def test_each_node_sits_in_one_queue(self):
        hosts = [n["host"] for q in self.q.values() for n in q["nodes"]]
        self.assertEqual(len(hosts), len(set(hosts)))

    def test_fleet_wide_crash_is_one_queue(self):
        self.assertEqual(len(self.q["common_mode"]["nodes"]), 300)
        self.assertEqual(self.q["common_mode"]["owner"], "engineering")

    def test_planted_faults_land_in_the_right_queue(self):
        for h in self.truth["sdc"]:
            self.assertEqual(self.where[h], "gpu_sdc", h)
        for h in self.truth["missing_gpu"]:
            self.assertEqual(self.where[h], "gpu_off_bus", h)
        for h in self.truth["app_bug"]:
            self.assertEqual(self.where[h], "software", h)
        for h in self.truth["unknown_xid"]:
            self.assertIn(self.where[h], ("unknown", "gpu_memory", "gpu_off_bus"), h)

    def test_lemons_lead_their_queue(self):
        q = self.q["gpu_memory"]["nodes"]
        lemons = [n["host"] for n in q if n["lemon"]]
        self.assertEqual(sorted(lemons), sorted(self.truth["lemons"]))
        self.assertTrue(all(n["lemon"] for n in q[:len(lemons)]))


if __name__ == "__main__":
    unittest.main()
