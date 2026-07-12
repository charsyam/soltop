import io
import subprocess
import sys
import unittest
import unittest.mock

import soltop


class _FakeKeys:
    """Stands in for KeyReader, replaying one canned read per frame."""

    def __init__(self, script):
        self.script = list(script)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read_available(self):
        return self.script.pop(0) if self.script else ""


class _FakeSampler:
    def read(self, interval):
        return {"gpu": [], "cpu": [], "power": {"SoC": 0.0},
                "power_max": {}, "power_avg": {}, "power_peak": {}}

    def close(self):
        pass


class _FakeProcSampler:
    def step(self, interval=None):
        return []


class LiveKeyTests(unittest.TestCase):
    def _run_live(self, keys):
        """Run live() against fakes, returning the process_only flag per frame."""
        seen = []
        real_render = soltop.render

        def spy(view, cols=80, gpu_hist=None, procs=None, height=None,
                soc_hist=None, process_only=False, single_sample=False):
            seen.append(process_only)
            return real_render(view, cols, gpu_hist, procs, height, soc_hist,
                               process_only, single_sample)

        with unittest.mock.patch.object(soltop, "Sampler", _FakeSampler), \
                unittest.mock.patch.object(soltop, "ProcGPUSampler", _FakeProcSampler), \
                unittest.mock.patch.object(soltop, "KeyReader", lambda s: _FakeKeys(keys)), \
                unittest.mock.patch.object(soltop.time, "sleep", lambda s: None), \
                unittest.mock.patch.object(soltop, "render", spy), \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            soltop.live(interval=0.01)
        return seen

    def test_q_quits_the_live_loop(self):
        # Without a 'q' handler this loop would never terminate.
        self.assertEqual(self._run_live(["", "q"]), [False])

    def test_each_p_toggles_the_process_view(self):
        self.assertEqual(self._run_live(["", "p", "", "p", "", "q"]),
                         [False, True, True, False, False])

    def test_two_p_presses_in_one_frame_return_to_the_dashboard(self):
        # The old count("p") % 2 logic silently swallowed an even number of
        # presses arriving in a single read; each press must toggle.
        self.assertEqual(self._run_live(["pp", "q"]), [False])


class ChannelSelectionTests(unittest.TestCase):
    """The GPU/CPU subgroups we subscribe to decide whether the numbers mean
    anything. 'GPU Stats' also exposes latched status registers (Fender State,
    the AFR/Boost controllers, CLTM) that sit pinned at 100%; averaging those
    into the GPU figure reported ~40% on a fully idle machine."""

    def test_status_register_subgroups_are_not_treated_as_utilization(self):
        for bogus in ("Fender State", "UV Warn State", "DVD Request States",
                      "CLTM-induced GPU Performance States",
                      "GPU Boost Controller Performance States",
                      "AFR Power Controller States", "GPU Power Controller States",
                      "PMU Loop Lost Performance Reason Code States",
                      "UT Engagement centi-% Histogram"):
            self.assertNotIn(bogus, soltop._UTIL_SUBGROUPS["gpu"], bogus)
            u = bogus.upper()
            picked = ("PERFORMANCE STATE" in u
                      and not any(b in u for b in soltop._NOT_UTIL))
            self.assertFalse(picked, f"fallback scan would wrongly pick {bogus!r}")

    def test_canonical_utilization_subgroups_are_selected(self):
        self.assertIn("GPU Performance States", soltop._UTIL_SUBGROUPS["gpu"])
        self.assertIn("CPU Core Performance States", soltop._UTIL_SUBGROUPS["cpu"])

    def test_fallback_scan_still_accepts_a_renamed_core_subgroup(self):
        # A rename must degrade gracefully, not select nothing.
        u = "GPU Core Performance States".upper()
        self.assertTrue("PERFORMANCE STATE" in u
                        and not any(b in u for b in soltop._NOT_UTIL))


class SamplerLifecycleTests(unittest.TestCase):
    def test_read_after_close_raises_instead_of_resurrecting(self):
        # read()'s dropped-subscription recovery cannot distinguish a dead
        # subscription from a deliberately closed one, so without a guard a
        # read() after close() silently re-subscribed and leaked a native
        # subscription that nobody would ever release.
        s = soltop.Sampler()
        try:
            s.read(0.05)
        finally:
            s.close()
        with self.assertRaises(RuntimeError):
            s.read(0.05)

    def test_close_is_idempotent(self):
        s = soltop.Sampler()
        s.close()
        s.close()  # must not raise or double-free

    def test_context_manager_releases_on_exception(self):
        with self.assertRaises(ValueError):
            with soltop.Sampler() as s:
                raise ValueError("boom")
        self.assertIsNone(s.prev)
        self.assertTrue(s.closed)


class SoltopLogicTests(unittest.TestCase):
    def test_active_ratio(self):
        self.assertEqual(soltop.active_ratio({"IDLE": 90, "P0": 10}), 0.1)
        self.assertEqual(soltop.active_ratio({"IDLE": 0, "P0": 100}), 1.0)
        self.assertEqual(soltop.active_ratio({}), 0.0)
        self.assertIsNone(soltop.active_ratio({"P0": 10, "P1": 20}))

    def test_cluster_frequency_weights_idle_at_the_bottom_of_the_ladder(self):
        # Idle residency counts at the ladder floor, so the reported clock is the
        # mean over the interval, not "the clock while awake" (which on Apple
        # Silicon is ~always the top step and pinned the display near 100%).
        ladder = [1000, 2000, 3000]
        # 100 idle @1000 + 20 @2000 + 20 @3000 -> (100000+40000+60000)/140
        cores = [{"states": {"IDLE": 100, "V1P1": 20, "V2P0": 20}}]
        self.assertAlmostEqual(soltop.cluster_freq_mhz(cores, ladder), 200000 / 140)
        # A fully parked cluster sits at the bottom of the ladder.
        parked = [{"states": {"IDLE": 100, "DOWN": 50}}]
        self.assertEqual(soltop.cluster_freq_mhz(parked, ladder), 1000)
        # A fully pegged cluster sits at the top.
        pegged = [{"states": {"V2P0": 100}}]
        self.assertEqual(soltop.cluster_freq_mhz(pegged, ladder), 3000)

    def test_pstate_index_reads_the_ascending_v_field(self):
        # CPU names are V<v>P<p> with v ascending and p descending, so
        # v + p == len(ladder) - 1. Reading the P suffix inverts the ladder:
        # V18P0 is the TOP step but parses as index 0, the ladder floor -- which
        # made a pegged CPU report its minimum clock.
        for name, want in (("V0P18", 0), ("V9P9", 9), ("V18P0", 18),
                           ("V0P6", 0), ("V6P0", 6)):
            self.assertEqual(soltop._pstate_index(name), want, name)

    def test_pstate_index_falls_back_to_a_plain_suffix(self):
        # GPU state names carry no V field.
        self.assertEqual(soltop._pstate_index("P3"), 3)
        self.assertIsNone(soltop._pstate_index("GPUPH"))
        self.assertIsNone(soltop._pstate_index(""))

    def test_top_dvfs_state_maps_to_the_top_of_the_ladder(self):
        ladder = [1000, 2000, 3000]          # 3 steps -> names V0P2, V1P1, V2P0
        top = [{"states": {"V2P0": 100}}]
        bottom = [{"states": {"V0P2": 100}}]
        self.assertEqual(soltop.cluster_freq_mhz(top, ladder), 3000)
        self.assertEqual(soltop.cluster_freq_mhz(bottom, ladder), 1000)

    def test_cluster_freq_reports_table_unit(self):
        cores = [{"states": {"V1P1": 10}}]      # V=1 -> ladder index 1
        mhz = {"values": [1000, 2000, 3000], "unit": "MHz"}
        self.assertEqual(soltop.cluster_freq(cores, mhz), (2000, "MHz"))
        # A scale-less CPU ladder must be reported as a percentage, never as MHz.
        pct = {"values": [50.0, 100.0], "unit": "%"}
        self.assertEqual(soltop.cluster_freq(cores, pct), (100.0, "%"))
        self.assertEqual(soltop.cluster_freq(cores, []), (0.0, "MHz"))

    def test_freq_txt_never_invents_mhz_for_a_scaleless_ladder(self):
        self.assertEqual(soltop._freq_txt(1398.0, "MHz"), "@ 1398 MHz")
        self.assertEqual(soltop._freq_txt(62.0, "%"), "@ 62% DVFS")
        self.assertEqual(soltop._freq_txt(0.0, "MHz"), "")

    def test_version(self):
        self.assertEqual(soltop.__version__, "0.4.2")

    def test_wrap_box_truncates_overlong_lines(self):
        long_line = "x" * 200
        boxed = soltop.wrap_box([long_line], cols=40)
        # Every row (borders included) must be exactly `cols` wide, or the right
        # border shifts and the box visibly breaks.
        for row in boxed:
            self.assertEqual(soltop._visible_len(row), 40, row)

    def test_wrap_box_truncation_keeps_color_from_bleeding(self):
        colored = "\x1b[91m" + "y" * 200
        boxed = soltop.wrap_box([colored], cols=40)
        self.assertEqual(soltop._visible_len(boxed[1]), 40)
        self.assertIn(soltop.RESET, boxed[1])

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

    def test_render_single_sample_omits_avg_and_peak(self):
        view = {
            "gpu_pct": 30.0,
            "clusters": [],
            "power": {"CPU": 1000.0, "SoC": 1000.0},
            "power_max": {}, "power_avg": {}, "power_peak": {},
        }
        frame = soltop.render(view, cols=100, procs=[], single_sample=True)
        self.assertIn("30.0%", frame)
        self.assertNotIn("avg", frame)
        self.assertNotIn("peak", frame)

    def test_render_long_process_name_does_not_break_the_box(self):
        procs = [{"pid": 1, "name": "A" * 300, "gpu_ms_s": 5.0}]
        frame = soltop.render({"gpu_pct": 0.0, "clusters": []}, cols=60,
                              procs=procs, process_only=True)
        for row in frame.replace("\x1b[K", "").splitlines():
            self.assertEqual(soltop._visible_len(row), 60, row)

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
