"""v0.3 draft tests: Dell iDRAC message IDs. Run: python -m unittest discover -s tests -v

(c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glyphrca.encode import Codebook, Encoder  # noqa: E402

CB = Codebook.load()
ENC = Encoder(CB)
T = "2026-10-13T14:00:01 "


class DellMessageIds(unittest.TestCase):
    def test_every_id_has_a_known_concept_and_a_source(self):
        self.assertGreaterEqual(sum("dell_severity" in v for v in CB.message_ids.values()), 60)
        for mid, rule in ((k, v) for k, v in CB.message_ids.items() if "dell_severity" in v):
            with self.subTest(mid=mid):
                self.assertIn(rule["concept"], CB.concepts)
                self.assertTrue(rule.get("text"))
                self.assertIn(rule.get("dell_severity"), ("Critical", "Warning", "Informational"))

    def test_id_decides_the_concept(self):
        cases = {
            "MEM0001 Multi-bit memory errors detected on a memory device at location(s) DIMM A2.": "MEM.ECC.UNCORR",
            "MEM0702 Correctable memory error rate exceeded for DIMM B1.": "MEM.ECC.CORR",
            "CPU0704 CPU 2 machine check error detected.": "CPU.MCE.LOGGED",
            "PSU0003 The power input for power supply 2 is lost.": "PSU.INPUT.LOST",
            "RDU0012 Lost PSU redundancy.": "PSU.REDUNDANCY.LOST",
            "FAN0001 Fan 3 RPM is less than the lower critical threshold.": "FAN.FAIL",
            "TMP0104 The system board Inlet Temp temperature is outside of range.": "THERMAL.OUT_OF_RANGE",
            "VLT0204 The system board PS1 PG FAIL voltage is outside of range.": "VOLT.OUT_OF_RANGE",
            "NIC100 The NIC Slot 4 Port 1 network link is down.": "NET.LINK.DOWN",
            "PDR16 Predictive failure reported for Disk 0 in Backplane 1.": "DISK.PREDICTIVE_FAIL",
            "PCI1320 A bus fatal error was detected on a component at bus 2 device 0 function 0.": "PCIE.FATAL",
            "PCI1360 A bus fatal error was detected on a component at slot 5.": "PCIE.FATAL",
            "PCI1302 A bus time-out was detected on a component at bus 64 device 2 function 0.": "PCIE.TIMEOUT",
            "UEFI0031 PCIe downtrain is detected on Slot 3. Expected link width: 16 Actual link width: 8.": "LINK.DEGRADED",
            "GPU0001 GPU_3 temperature 92 above Upper Critical Temperature 90.": "GPU.THERMAL.CRIT",
            "HWC9052 The health of Graphics Processing Unit (GPU) device 2 is critical because of single or double bit errors on the memory cells.": "GPU.MEM.HEALTH.CRIT",
            "ACC0001 The Graphic Processing Unit (GPU) baseboard has saved log data related to critical events.": "GPU.BASEBOARD.CRIT_LOG",
        }
        for line, concept in cases.items():
            with self.subTest(line=line):
                e = ENC.encode(T + line)
                self.assertEqual(e.concept, concept)
                self.assertEqual(e.args[0], "msg=" + line.split()[0])
                self.assertIsNotNone(e.ts)

    def test_dell_pcie_fatal_text_without_an_id(self):
        e = ENC.encode(T + "A bus fatal error was detected on a component at slot 5.")
        self.assertEqual(e.concept, "PCIE.FATAL")
        self.assertIn("slot=5", e.args)
        e = ENC.encode(T + "A fatal error was detected on a component at bus 2 device 0 function 0.")
        self.assertEqual(e.concept, "PCIE.FATAL")
        self.assertIn("pci=bus2.dev0.fn0", e.args)

    def test_hex_bdf_and_gpu_index_args(self):
        e = ENC.encode(T + "UEFI0031 PCIe downtrain is detected on Slot 36 (Bus:0x5B Dev:0x02 F:0x00). "
                           "Expected link width: 16 Actual link width: 8.")
        self.assertIn("bdf=0000:5b:02.0", e.args)
        self.assertIn("slot=36", e.args)
        self.assertIn("gpu=3", ENC.encode(T + "GPU0001 GPU_3 temperature 92 above Upper Critical Temperature 90.").args)

    def test_idrac_slot_binds_bmc_gpu_events_to_the_card(self):
        from glyphrca.health import gpu_health
        gpus = {("h", "0000:1b:00.0"): {"host": "h", "bdf": "0000:1b:00.0", "hours": "", "idrac_slot": "GPU_2"},
                ("h", "0000:43:00.0"): {"host": "h", "bdf": "0000:43:00.0", "hours": "", "idrac_slot": "GPU_3"}}
        ev = [ENC.encode(T + "HWC9052 The health of Graphics Processing Unit (GPU) device 2 is critical because of "
                             "single or double bit errors on the memory cells.")]
        cards = {c["bdf"]: c for c in gpu_health({"h": ev}, {}, gpus, set())}
        self.assertEqual(cards["0000:1b:00.0"]["breakdown"].get("memory"), 15)
        self.assertNotIn("memory", cards["0000:43:00.0"]["breakdown"])
        # without idrac_slot the event reaches no card, rather than a guessed one
        for g in gpus.values():
            g.pop("idrac_slot")
        cards = gpu_health({"h": ev}, {}, gpus, set())
        self.assertTrue(all("memory" not in c["breakdown"] for c in cards))

    def test_cpu0000_is_not_read_as_ierr(self):
        self.assertNotIn("CPU0000", CB.message_ids)  # guide: informational "check for additional logs"

    def test_unknown_id_falls_back_to_words(self):
        e = ENC.encode(T + "XYZ9999 PCIe correctable error on device 0000:41:00.0")
        self.assertEqual(e.concept, "PCIE.CORR")


if __name__ == "__main__":
    unittest.main()
