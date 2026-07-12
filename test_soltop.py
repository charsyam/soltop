import subprocess
import sys
import unittest

import soltop


class SoltopLogicTests(unittest.TestCase):
    def test_active_ratio(self):
        self.assertEqual(soltop.active_ratio({"IDLE": 90, "P0": 10}), 0.1)
        self.assertEqual(soltop.active_ratio({"IDLE": 0, "P0": 100}), 1.0)
        self.assertEqual(soltop.active_ratio({}), 0.0)
        self.assertIsNone(soltop.active_ratio({"P0": 10, "P1": 20}))

    def test_cluster_frequency_ignores_idle_residency(self):
        cores = [{"states": {"IDLE": 100, "V0P1": 20, "V0P2": 20}}]
        self.assertEqual(soltop.cluster_freq_mhz(cores, [1000, 2000, 3000]), 2500)

    def test_version(self):
        self.assertEqual(soltop.__version__, "0.3.0")

    def test_render_fits_short_terminal_height(self):
        view = {
            "gpu_pct": 0.0,
            "clusters": [],
            "power": {},
            "power_max": {},
            "power_avg": {},
            "power_peak": {},
        }
        frame = soltop.render(view, cols=80, gpu_hist=[0.0], procs=[],
                              height=10, soc_hist=[0.0])
        self.assertEqual(len(frame.splitlines()), 10)

    def test_render_process_only_view(self):
        view = {"gpu_pct": 12.0, "clusters": []}
        procs = [{"pid": 42, "name": "MetalApp", "gpu_ms_s": 125.0}]
        frame = soltop.render(view, cols=80, procs=procs, height=10,
                              process_only=True)
        self.assertEqual(len(frame.splitlines()), 10)
        self.assertIn("MetalApp", frame)
        self.assertNotIn("GPU Usage:", frame)

    def test_cli_rejects_nonpositive_interval(self):
        for value in ("0", "-1", "nan", "inf"):
            result = subprocess.run(
                [sys.executable, "soltop.py", "--interval", value, "--once"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2, value)

    def test_cli_rejects_negative_columns(self):
        result = subprocess.run(
            [sys.executable, "soltop.py", "--cols", "-1", "--once"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
