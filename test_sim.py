#!/usr/bin/env python3
"""Offline self-tests for `sim.py` and `probe.py` (zero external dependencies)."""
import json, os, unittest
import probe, sim

HERE = os.path.dirname(os.path.abspath(__file__))


class StandaloneSimTests(unittest.TestCase):
    def test_01_x86_and_arm64_kernel_builders(self):
        lx, ex = probe.build_x86_kernels(include_avx512=True)
        self.assertEqual(len(lx), 7)
        self.assertEqual(len(ex), 22)
        la, ea = probe.build_arm64_kernels()
        self.assertEqual(len(la), 7)
        self.assertEqual(len(ea), 21)

    def test_02_static_lints_and_cross_arch_table(self):
        _, ex = probe.build_x86_kernels(include_avx512=True)
        by_name = {k["name"]: k for k in ex}
        nf = by_name["probe_nofusion_sep"]["loop_ops"]
        lints = sim.static_lint_loop(nf, "zen5")
        self.assertIn("broken_macro_fusion", [x["kind"] for x in lints])
        tbl = sim.compare_all_archs(by_name["real_sse2_g_round"]["loop_ops"], "x86_64")
        self.assertEqual([r["arch"] for r in tbl], ["zen5", "zen4", "zen3", "icelake"])
        # Zen 5 SSE2 128-bit vector ops have 2-cycle latency vs 1-cycle on Zen 4 / Ice Lake
        by_arch = {r["arch"]: r["cycles_per_iter"] for r in tbl}
        self.assertGreater(by_arch["zen5"], by_arch["zen4"] * 1.5)

    def test_03_bundled_hardware_corpus_sanity(self):
        res_dir = os.path.join(HERE, "results")
        expected = [
            ("amd-epyc-9255-zen5.json", 70.0),
            ("amd-ryzen-9700x-zen5.json", 70.0),
            ("amd-threadripper-7975wx-zen4.json", 75.0),
            ("amd-epyc-7443-zen3.json", 75.0),
            ("intel-xeon-6330-icelake.json", 70.0),
            ("apple-m6-super.json", 70.0),
            ("ampere-ampereone.json", 85.0),
        ]
        for fname, min_pct in expected:
            with open(os.path.join(res_dir, fname), "r", encoding="utf-8") as f:
                d = json.load(f)
            acc = d["uica_sim_accuracy"]
            self.assertGreaterEqual(acc["within_10pct_rate"], min_pct, f"{fname}: {acc}")
            self.assertLess(acc["mean_abs_err_pct"], 9.0, f"{fname}: {acc}")


if __name__ == "__main__":
    unittest.main()
