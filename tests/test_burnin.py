import unittest

from glyphrca.burnin import build_records, count_xid, parse_smi_csv, summarize

HEADER = ("timestamp, index, uuid, power.draw [W], power.limit [W], pcie.link.width.current, "
          "pcie.link.width.max, pcie.link.gen.current, pcie.link.gen.max, clocks.sm [MHz], "
          "temperature.gpu, ecc.errors.corrected.volatile.total, "
          "ecc.errors.uncorrected.volatile.total, clocks_throttle_reasons.active")


def row(t, uuid, power=300, width=16, gen=5, sm=1980, temp=70, corr=0, uncorr=0, throttle="0x0000000000000000"):
    return (f"2026/10/27 10:00:{t:02d}, 0, {uuid}, {power}, 350, {width}, 16, {gen}, 5, "
            f"{sm}, {temp}, {corr}, {uncorr}, {throttle}")


def capture(rows):
    return "\n".join([HEADER] + rows) + "\n"


class BurnInTests(unittest.TestCase):
    def test_parse_groups_by_uuid_and_strips_units(self):
        text = capture([row(0, "GPU-A"), row(1, "GPU-B"), row(2, "GPU-A")])
        by_gpu = parse_smi_csv(text)
        self.assertEqual(set(by_gpu), {"GPU-A", "GPU-B"})
        self.assertIn("power.draw", by_gpu["GPU-A"][0])
        self.assertEqual(len(by_gpu["GPU-A"]), 2)

    def test_steady_state_drops_warmup(self):
        samples = [{"uuid": "GPU-A", "power.draw": "100", "clocks.sm": "500"}] * 3 + \
                  [{"uuid": "GPU-A", "power.draw": "300", "clocks.sm": "1980"}] * 5
        rec = summarize(samples, warmup_s=3)
        self.assertEqual(rec["signals"]["power_draw_w_p50"], 300)
        self.assertEqual(rec["signals"]["sm_clock_mhz_steady"], 1980)

    def test_ecc_uncorrected_increase_is_flagged(self):
        samples = parse_smi_csv(capture([row(0, "GPU-A", uncorr=0), row(1, "GPU-A", uncorr=2)]))["GPU-A"]
        rec = summarize(samples, warmup_s=0)
        self.assertEqual(rec["signals"]["ecc_uncorrected_delta"], 2)
        self.assertIn("ecc_uncorrected", rec["flags"])

    def test_corrected_ecc_is_recorded_not_flagged(self):
        samples = parse_smi_csv(capture([row(0, "GPU-A", corr=10), row(1, "GPU-A", corr=50)]))["GPU-A"]
        rec = summarize(samples, warmup_s=0)
        self.assertEqual(rec["signals"]["ecc_corrected_delta"], 40)
        self.assertEqual(rec["flags"], [])

    def test_degraded_pcie_width_is_flagged(self):
        samples = parse_smi_csv(capture([row(0, "GPU-A", width=8), row(1, "GPU-A", width=8)]))["GPU-A"]
        rec = summarize(samples, warmup_s=0)
        self.assertEqual(rec["signals"]["pcie_width_max_under_load"], 8)
        self.assertEqual(rec["signals"]["pcie_width_expected"], 16)
        self.assertIn("pcie_width_degraded", rec["flags"])

    def test_healthy_link_has_no_flag(self):
        samples = parse_smi_csv(capture([row(0, "GPU-A"), row(1, "GPU-A")]))["GPU-A"]
        rec = summarize(samples, warmup_s=0)
        self.assertEqual(rec["flags"], [])

    def test_not_supported_values_become_none(self):
        text = capture([
            "2026/10/27 10:00:00, 0, GPU-A, [N/A], 350, 16, 16, 5, 5, 1980, 70, [N/A], [N/A], 0x0"])
        samples = parse_smi_csv(text)["GPU-A"]
        rec = summarize(samples, warmup_s=0)
        self.assertIsNone(rec["signals"]["power_draw_w_p50"])
        self.assertIsNone(rec["signals"]["ecc_corrected_delta"])

    def test_throttle_reasons_collected(self):
        samples = parse_smi_csv(capture([row(0, "GPU-A", throttle="0x0000000000000004"),
                                         row(1, "GPU-A", throttle="0x0000000000000000")]))["GPU-A"]
        rec = summarize(samples, warmup_s=0)
        self.assertEqual(set(rec["signals"]["throttle_reasons"]),
                         {"0x0000000000000004", "0x0000000000000000"})

    def test_xid_count_from_kernel_log(self):
        log = ("Oct 27 10:00:01 host kernel: NVRM: Xid (PCI:0000:41:00): 79, GPU has fallen off the bus\n"
               "Oct 27 10:00:02 host kernel: usb 1-1: new device\n"
               "Oct 27 10:00:05 host kernel: NVRM: Xid (PCI:0000:42:00): 13, Graphics Exception\n")
        self.assertEqual(count_xid(log), 2)

    def test_build_records_one_per_card(self):
        text = capture([row(0, "GPU-A"), row(0, "GPU-B")])
        recs = build_records(text, kernel_log="", warmup_s=0)
        self.assertEqual({r["gpu_uuid"] for r in recs}, {"GPU-A", "GPU-B"})
        self.assertEqual(recs[0]["signals"]["xid_count_capture"], 0)


if __name__ == "__main__":
    unittest.main()
