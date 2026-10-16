"""v0.2 tests: Linux kernel road, cross-source evidence, software-wide failures, lemon nodes.
Run: python -m unittest discover -s tests -v

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glyphrca.encode import Codebook, Encoder, parse_ts  # noqa: E402
from glyphrca.report import analyse_fleet  # noqa: E402
from glyphrca.synth import generate  # noqa: E402

ENC = Encoder(Codebook.load())
DMESG = "[Tue Oct 13 14:00:01 2026] "
JOURNAL = "2026-10-13T14:00:01+0000 node-7 kernel: "


class KernelRoad(unittest.TestCase):
    def test_both_timestamp_formats(self):
        self.assertEqual(parse_ts(DMESG + "x"), parse_ts(JOURNAL + "x"))
        self.assertEqual(parse_ts(DMESG + "x"), 1791900001.0)

    def test_xid_table_follows_nvidia_catalog(self):
        cases = {79: "GPU.BUS.LOST", 48: "GPU.MEM.ECC.DBE", 94: "GPU.MEM.ECC.CONTAINED",
                 95: "GPU.MEM.ECC.UNCONTAINED", 92: "GPU.MEM.ECC.SBE_RATE", 63: "GPU.MEM.REMAP.EVENT",
                 64: "GPU.MEM.REMAP.FAIL", 74: "LINK.NVLINK.ERROR", 119: "GPU.CTRL.GSP", 120: "GPU.CTRL.GSP",
                 13: "GPU.EXEC.EXCEPTION", 31: "GPU.EXEC.EXCEPTION", 45: "GPU.DRIVER.CLEANUP"}
        for code, concept in cases.items():
            for prefix, form in ((DMESG, "PCI:0000:41:00"), (JOURNAL, "0000:41:00")):
                with self.subTest(xid=code, form=form):
                    e = ENC.encode_kernel(f"{prefix}NVRM: Xid ({form}): {code}, pid=1, details")
                    self.assertEqual(e.concept, concept)
                    self.assertEqual(e.args[:2], ("bdf=0000:41:00.0", f"xid={code}"))
                    self.assertEqual(e.source, "kernel")

    def test_unknown_xid_is_a_data_gap_not_a_guess(self):
        e = ENC.encode_kernel(DMESG + "NVRM: Xid (PCI:0000:41:00): 154, GPU recovery action changed")
        self.assertEqual(e.concept, "GPU.XID.OTHER")
        self.assertIn("xid=154", e.args)

    def test_other_kernel_events(self):
        cases = [
            ("nvidia-nvswitch2: SXid (PCI:0000:c5:00.0): 12028, Non-fatal, Link 30", "LINK.NVSWITCH.ERROR"),
            ("EDAC MC0: 1 CE memory read error on CPU_SrcID#0_MC#0_Chan#1_DIMM#0 (channel:1 slot:0)", "MEM.ECC.CORR"),
            ("EDAC MC1: 1 UE memory read error on CPU_SrcID#1_MC#1_Chan#0_DIMM#1 (channel:0 slot:1)", "MEM.ECC.UNCORR"),
            ("nvidia 0000:43:00.0: PCIe Bus Error: severity=Corrected, type=Physical Layer, (Receiver ID)", "PCIE.CORR"),
            ("pcieport 0000:40:01.0: PCIe Bus Error: severity=Uncorrected (Fatal), type=Transaction Layer", "PCIE.FATAL"),
            ("pcieport 0000:40:01.0: PCIe Bus Error: severity=Uncorrected (Non-Fatal), type=Transaction Layer",
             "PCIE.UNCORR.NONFATAL"),
            ("pcieport 0000:40:01.0: pciehp: Slot(3): Link Down", "PCIE.LINK.DOWN"),
            ("mce: [Hardware Error]: CPU 3: Machine Check: 0 Bank 5: be00000000800400", "CPU.MCE.LOGGED"),
            ("Kernel panic - not syncing: Fatal machine check", "OS.KERNEL.PANIC"),
            ("watchdog: BUG: soft lockup - CPU#3 stuck for 22s! [python3:1234]", "OS.SOFT_LOCKUP"),
            ("INFO: task kworker/2:1:123 blocked for more than 120 seconds.", "OS.HUNG_TASK"),
            ("Out of memory: Killed process 4242 (python3)", "OS.OOM"),
            ("CPU3: Core temperature above threshold, cpu clock throttled (total events = 1)", "THERMAL.THROTTLE"),
            ("mlx5_core 0000:9a:00.0 ib0: Link DOWN", "NET.LINK.DOWN"),
        ]
        for msg, concept in cases:
            with self.subTest(msg=msg):
                self.assertEqual(ENC.encode_kernel(DMESG + msg).concept, concept)
        e = ENC.encode_kernel(DMESG + cases[1][0])
        self.assertIn("dimm=CPU_SrcID#0_MC#0_Chan#1_DIMM#0", e.args)
        e = ENC.encode_kernel(DMESG + cases[3][0])
        self.assertIn("bdf=0000:43:00.0", e.args)

    def test_routine_kernel_lines_are_ignored(self):
        for msg in ("Linux version 6.8.0-45-generic", "NVRM: loading NVIDIA UNIX x86_64 Kernel Module 560.35.03",
                    "systemd[1]: Started Daily apt download activities."):
            self.assertIsNone(ENC.encode_kernel(DMESG + msg))


class FleetV02(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.truth = generate(cls.dir, bad=300, ok=150, flood=(10, 20), seed=13)
        cls.r = analyse_fleet(cls.dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def test_survivor_still_found_with_two_interleaved_sources(self):
        self.assertEqual([s["host"] for s in self.r["survivors"]], [self.truth["survivor"]])
        storm = self.r["signatures"][0]
        self.assertEqual(storm["servers"], 299)  # SEL and kernel interleave, the story stays one signature
        self.assertIn("CPU.MCE.LOGGED", storm["concepts"])

    def test_one_event_three_witnesses(self):
        hosts = self.r["bus_loss"][self.truth["missing_slot"]]
        self.assertEqual(sorted(hosts), sorted(self.truth["missing_gpu"]))
        for witnesses in hosts.values():
            self.assertEqual(witnesses, ["kernel", "pcie_tree", "sel"])

    def test_application_bug_is_not_blamed_on_gpus(self):
        sw = self.r["software_wide"]
        self.assertEqual([s["concept"] for s in sw], ["GPU.EXEC.EXCEPTION"])
        self.assertEqual(sorted(sw[0]["hosts"]), sorted(self.truth["app_bug"]))
        self.assertLessEqual(sw[0]["window_s"], 30)

    def test_lemon_nodes(self):
        self.assertEqual(sorted(self.r["lemon_nodes"]), sorted(self.truth["lemons"]))

    def test_unknown_xid_reported_as_gap(self):
        self.assertEqual(self.r["unknown_xid"], {"xid=154": len(self.truth["unknown_xid"])})

    def test_repeated_light_triage_escalates(self):
        esc = [e for e in self.r["escalations"] if e["concept"] == "GPU.MEM.ECC.CONTAINED"]
        self.assertEqual(sorted(e["host"] for e in esc), sorted(self.truth["escalate"]))
        self.assertTrue(all(e["per_day"] == 5 and e["to"] == "drain_and_cordon_and_diagnose" for e in esc))
        gsp = [e["host"] for e in self.r["escalations"] if e["concept"] == "GPU.CTRL.GSP"]
        self.assertEqual(gsp, self.truth["gsp_repeat"])

    def test_practitioner_triage(self):
        cb = Codebook.load()
        self.assertEqual(cb.triage("GPU.MEM.ECC.UNCONTAINED"), "pcie_reset_or_reboot")
        self.assertEqual(cb.triage("GPU.MEM.REMAP.FAIL"), "drain_and_cordon_then_rma")
        self.assertEqual(cb.triage("GPU.EXEC.EXCEPTION"), "return_logs_to_user")
        self.assertEqual(cb.concepts["GPU.MEM.REMAP.EVENT"]["deadline_h"], 12)

    def test_telemetry_early_warnings_from_both_metric_dialects(self):
        got = {c: sorted(h) for c, h in self.r["telemetry"].items()}
        for concept, hosts in self.truth["telemetry"].items():
            self.assertEqual(got.get(concept), sorted(hosts), concept)
        self.assertNotIn("GPU.THERMAL.THROTTLE", got)  # control hosts stay quiet

    def test_zombie_fabric_orphan_iommu_storm_init(self):
        self.assertEqual(sorted(self.r["zombies"]), self.truth["zombie"])
        self.assertEqual(self.r["fabric_manager_down"], sorted(self.truth["fabric_manager"]))
        self.assertEqual([o["host"] for o in self.r["orphan_cleanups"]], self.truth["orphan_xid45"])
        self.assertEqual(self.r["iommu_faults"], self.truth["iommu"])
        self.assertEqual(sorted(self.r["aer_storms"]), self.truth["aer_storm"])
        self.assertEqual(self.r["gpu_init_failures"], self.truth["init_fail"])

    def test_gsp_is_driver_or_hardware_not_user_app(self):
        cb = Codebook.load()
        self.assertEqual(cb.cause("GPU.CTRL.GSP"), "driver_or_hw")
        self.assertEqual(cb.triage("GPU.CTRL.GSP"), "reset_gpu_then_watch")

    def test_v01_findings_unchanged(self):
        self.assertEqual([(f["attribute"], f["value"]) for f in self.r["inventory"]], [("fw", "3.2.1")])
        self.assertEqual(set(self.r["pcie"]["upstream_suspects"]), {self.truth["bad_switch"]})


if __name__ == "__main__":
    unittest.main()
