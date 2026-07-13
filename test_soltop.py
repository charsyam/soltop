import io
import subprocess
import sys
import time
import unittest
import unittest.mock

import soltop
from soltop import cli as soltop_cli
from soltop import ui as soltop_ui
from soltop.core import dvfs as soltop_dvfs
from soltop.core import power as soltop_power
from soltop.core import sampler as soltop_sampler
from soltop.core import process as soltop_proc
from soltop.core import temps as soltop_temps
from soltop.exporter import formats as soltop_formats


def _has_ioreport():
    """True if this machine exposes real IOReport CPU/GPU state channels.

    A virtualised macOS runner (GitHub Actions) is arm64 and has the IOReport
    library, but no GPU/CPU state channels behind it -- so anything constructing
    a real Sampler cannot run there. Those tests skip rather than fail: they are
    reporting the absence of hardware, not a defect.
    """
    try:
        soltop.Sampler().close()
        return True
    except Exception:
        return False


HAS_IOREPORT = _has_ioreport()
needs_hardware = unittest.skipUnless(
    HAS_IOREPORT, "no real IOReport CPU/GPU state channels (virtualised host?)")


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
                "power_avg": {}, "power_peak": {}}

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
                soc_hist=None, temp_hist=None, process_only=False,
                single_sample=False, core_only=False, temp_only=False):
            seen.append(process_only)
            return real_render(view, cols, gpu_hist, procs, height, soc_hist,
                               temp_hist, process_only, single_sample, core_only,
                               temp_only)

        with unittest.mock.patch.object(soltop_ui, "Sampler", _FakeSampler), \
                unittest.mock.patch.object(soltop_ui, "ProcGPUSampler", _FakeProcSampler), \
                unittest.mock.patch.object(soltop_ui, "KeyReader", lambda s: _FakeKeys(keys)), \
                unittest.mock.patch.object(soltop_ui.time, "sleep", lambda s: None), \
                unittest.mock.patch.object(soltop_ui, "render", spy), \
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

    def _run_live_views(self, keys):
        """Run live() against fakes, returning which view each frame drew."""
        seen = []
        real_render = soltop.render

        def spy(view, cols=80, gpu_hist=None, procs=None, height=None,
                soc_hist=None, temp_hist=None, process_only=False,
                single_sample=False, core_only=False, temp_only=False):
            seen.append("proc" if process_only
                        else "core" if core_only
                        else "temp" if temp_only else "dash")
            return real_render(view, cols, gpu_hist, procs, height, soc_hist,
                               temp_hist, process_only, single_sample, core_only,
                               temp_only)

        with unittest.mock.patch.object(soltop_ui, "Sampler", _FakeSampler), \
                unittest.mock.patch.object(soltop_ui, "ProcGPUSampler", _FakeProcSampler), \
                unittest.mock.patch.object(soltop_ui, "KeyReader", lambda s: _FakeKeys(keys)), \
                unittest.mock.patch.object(soltop_ui.time, "sleep", lambda s: None), \
                unittest.mock.patch.object(soltop_ui, "render", spy), \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            soltop.live(interval=0.01)
        return seen

    def test_c_toggles_the_per_core_view_and_back(self):
        self.assertEqual(self._run_live_views(["", "c", "", "c", "", "q"]),
                         ["dash", "core", "core", "dash", "dash"])

    def test_t_toggles_the_temperature_view_and_back(self):
        # The temperature graph costs 7 rows; on a short terminal that pushed CPU
        # and memory off the dashboard. The dashboard keeps a one-line summary
        # and the graph lives behind 't'.
        self.assertEqual(self._run_live_views(["", "t", "", "t", "", "q"]),
                         ["dash", "temp", "temp", "dash", "dash"])

    def test_all_three_views_are_mutually_exclusive(self):
        # Pressing one view key from another switches rather than stacking.
        self.assertEqual(self._run_live_views(["t", "p", "c", "t", "q"]),
                         ["temp", "proc", "core", "temp"])

    def test_core_and_process_views_are_mutually_exclusive(self):
        # 'c' from the process view switches to cores rather than stacking, and
        # 'p' from the core view switches back.
        # Keys are handled before each frame is drawn, and 'q' returns without
        # drawing, so four reads produce three frames.
        self.assertEqual(self._run_live_views(["p", "c", "p", "q"]),
                         ["proc", "core", "proc"])


class LiveRecoveryTests(unittest.TestCase):
    @needs_hardware
    def test_live_survives_a_transient_sampler_failure(self):
        # read() closes the Sampler and raises when IOReport keeps failing. live()
        # caught only KeyboardInterrupt, so that hardening turned a transient
        # hiccup -- previously recovered from silently -- into a traceback that
        # killed the monitor.
        calls = {"n": 0}
        real_read = soltop.Sampler.read

        def flaky(sampler, interval=1.0):
            calls["n"] += 1
            if calls["n"] == 2:
                sampler.close()
                raise RuntimeError("samples failed")
            return real_read(sampler, interval)

        class Keys:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read_available(self):
                return "q" if calls["n"] > 3 else ""

        with unittest.mock.patch.object(soltop.Sampler, "read", flaky), \
                unittest.mock.patch.object(soltop_ui, "KeyReader", lambda s: Keys()), \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            soltop.live(interval=0.01)      # must not raise
        self.assertGreater(calls["n"], 2, "should have sampled past the failure")


    @needs_hardware
    def test_live_gives_up_instead_of_spinning_on_a_persistent_failure(self):
        # Retrying forever would rebuild the subscription (an
        # IOReportCopyAllChannels scan over ~11k channels) several times a second
        # behind a frozen screen, forever, telling the user nothing. Bound it.
        calls = {"n": 0}

        def always_fail(sampler, interval=1.0):
            calls["n"] += 1
            sampler.close()
            raise RuntimeError("samples failed")

        class Keys:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read_available(self):
                return ""

        with unittest.mock.patch.object(soltop.Sampler, "read", always_fail), \
                unittest.mock.patch.object(soltop_ui, "KeyReader", lambda s: Keys()), \
                unittest.mock.patch.object(soltop_ui.time, "sleep", lambda s: None), \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(RuntimeError):
                soltop.live(interval=0.01)
        self.assertLessEqual(calls["n"], soltop.LIVE_MAX_RETRIES + 1)

    def test_main_reports_a_sampler_failure_without_a_traceback(self):
        def boom(*a, **k):
            raise RuntimeError("subscription failed")

        err = io.StringIO()
        with unittest.mock.patch.object(soltop_cli, "Sampler", boom), \
                unittest.mock.patch.object(sys, "argv", ["soltop", "--once"]), \
                unittest.mock.patch.object(sys, "stderr", err):
            self.assertEqual(soltop.main(), 1)
        self.assertIn("subscription failed", err.getvalue())


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
        for renamed in ("GPU Cluster Performance States",
                        "CPU Cluster Performance States",
                        "ECPU Performance States"):
            self.assertTrue(
                soltop._fallback_state_subgroups(
                    [("GPU Stats" if renamed.startswith("GPU") else "CPU Stats",
                      renamed)],
                    "gpu" if renamed.startswith("GPU") else "cpu"),
                renamed)

    def test_fallback_returns_at_most_one_subgroup(self):
        # Several subgroups match the filter (this M4 Pro has GPU Performance
        # States, AFR Performance States AND GPU Software Performance States).
        # Subscribing to all of them would average them into one figure -- the
        # very bug that made an idle GPU read ~40%.
        seen = [("GPU Stats", "GPU Performance States"),
                ("GPU Stats", "AFR Performance States"),
                ("GPU Stats", "GPU Software Performance States")]
        picked = soltop._fallback_state_subgroups(seen, "gpu")
        self.assertEqual(len(picked), 1)
        # AFR is the display-refresh channel, not GPU compute: it must not win.
        self.assertEqual(picked[0][1], "GPU Performance States")

    def test_fallback_prefers_the_plain_channel_over_a_qualified_variant(self):
        seen = [("GPU Stats", "GPU Software Performance States"),
                ("GPU Stats", "GPU Performance States")]
        self.assertEqual(soltop._fallback_state_subgroups(seen, "gpu")[0][1],
                         "GPU Performance States")

    def test_fallback_still_rejects_aggregates_and_status_registers(self):
        seen = [("CPU Stats", "CPU Complex Performance States"),
                ("GPU Stats", "GPU Boost Controller Performance States"),
                ("GPU Stats", "Fender State")]
        self.assertEqual(soltop._fallback_state_subgroups(seen, "cpu"), [])
        self.assertEqual(soltop._fallback_state_subgroups(seen, "gpu"), [])

    def test_fallback_is_per_kind_not_all_or_nothing(self):
        # If only ONE canonical name is renamed, a global "is anything found?"
        # check would see the surviving one, skip the fallback, and drop that
        # whole subsystem (CPU or GPU) from the display entirely.
        seen = [("GPU Stats", "GPU Performance States"),          # canonical
                ("CPU Stats", "CPU Cluster Performance States")]  # renamed
        available = []
        for kind, names in soltop._UTIL_SUBGROUPS.items():
            found = [(g, sg) for g, sg in seen
                     if soltop.classify_group(g) == kind and sg in names]
            if not found:
                found = soltop._fallback_state_subgroups(seen, kind)
            available.extend(found)
        kinds = {soltop.classify_group(g) for g, _ in available}
        self.assertEqual(kinds, {"gpu", "cpu"}, available)


def _core_view(n_e=4, n_p=10):
    def cores(prefix, n):
        return [{"name": f"{prefix}{i:03d}", "pct": 10.0 * i, "mhz": 50.0,
                 } for i in range(n)]
    return {
        "gpu_pct": 0.0,
        "clusters": [
            {"key": "E", "label": "CPU E-cluster", "avg": 20.0, "count": n_e,
             "mhz": 60.0, "per_core": cores("ECPU", n_e)},
            {"key": "P", "label": "CPU P-cluster", "avg": 30.0, "count": n_p,
             "mhz": 70.0, "per_core": cores("PCPU", n_p)},
        ],
    }


class VGraphTests(unittest.TestCase):
    def _labels(self, **kw):
        rows = [soltop._ANSI_RE.sub("", r) for r in soltop.vgraph(**kw)]
        return [r.split("│")[0].strip() for r in rows if "│" in r]

    def test_graph_height_is_even_so_a_true_50_percent_row_exists(self):
        # A row's value is (level+1)/height, so only an even height puts a row
        # exactly on 50%. At the old height of 5 the rows sat at 20/40/60/80/100
        # and there was no halfway row to label.
        self.assertEqual(soltop.GRAPH_HEIGHT % 2, 0)

    def test_the_half_scale_row_is_labelled_50_percent(self):
        labels = self._labels(history=[10, 50, 90], height=soltop.GRAPH_HEIGHT,
                              width=12)
        self.assertEqual(labels[0], "100%")
        self.assertIn("50%", labels)

    def test_label_max_rescales_the_axis_to_half_of_full_scale(self):
        labels = self._labels(history=[50], height=soltop.GRAPH_HEIGHT, width=8,
                              label_max=110.0, label_unit="W")
        self.assertEqual(labels[0], "110W")
        self.assertIn("55W", labels)      # half of the 110 W full scale

    def test_an_odd_height_labels_no_halfway_row_rather_than_lying(self):
        # There is no 50% row at height=5, so none is labelled -- the label must
        # never be attached to a row that does not actually mean 50%.
        labels = self._labels(history=[50], height=5, width=8)
        self.assertEqual(labels[0], "100%")
        self.assertNotIn("50%", labels)


class GaugeStyleTests(unittest.TestCase):
    def test_gauge_uses_a_solid_fill_not_the_eighth_block(self):
        # '▏' paints only 1/8 of a cell, so the bar reads as washed out whatever
        # colour it is given. The fill must be a solid block.
        g = soltop.gauge_bar(0.5, 10)
        self.assertIn("█", g)
        self.assertIn("░", g)
        self.assertNotIn("▏", g)

    def test_gauge_colors_are_bold(self):
        for pct, code in ((10, "1;92"), (60, "1;93"), (90, "1;91")):
            self.assertEqual(soltop.color_for(pct), f"\x1b[{code}m")

    def test_gauge_fill_tracks_the_fraction(self):
        self.assertEqual(soltop.gauge_bar(0.0, 8).count("█"), 0)
        self.assertEqual(soltop.gauge_bar(1.0, 8).count("█"), 8)
        self.assertEqual(soltop.gauge_bar(0.5, 8).count("█"), 4)
        # Out-of-range fractions clamp rather than overflow the bar.
        self.assertEqual(soltop.gauge_bar(2.0, 8).count("█"), 8)
        self.assertEqual(soltop.gauge_bar(-1.0, 8).count("█"), 0)


class CoreViewTests(unittest.TestCase):
    def test_every_core_is_listed(self):
        frame = soltop.render(_core_view(), cols=90, procs=[], core_only=True)
        for name in ("ECPU000", "ECPU003", "PCPU000", "PCPU009"):
            self.assertIn(name, frame)
        # The dashboard's cluster gauges must not appear in this view.
        self.assertNotIn("GPU Usage:", frame)

    def test_per_core_percent_and_freq_are_not_truncated_away(self):
        # The bar must leave room for the value columns, or wrap_box() clips them.
        frame = soltop.render(_core_view(), cols=90, procs=[], core_only=True)
        self.assertIn("MHz", frame)
        self.assertIn("30.00%", frame)   # ECPU003 -> pct 30.0

    def test_core_view_fits_the_frame_and_keeps_the_box_intact(self):
        for cols in (40, 60, 100):
            for height in (6, 12, 30):
                frame = soltop.render(_core_view(), cols=cols, procs=[],
                                      height=height, core_only=True)
                rows = frame.replace("\x1b[K", "").splitlines()
                self.assertEqual(len(rows), height, (cols, height))
                for row in rows:
                    self.assertEqual(soltop._visible_len(row), cols, (cols, height))

    def test_core_view_without_clusters_does_not_crash(self):
        frame = soltop.render({"gpu_pct": 0.0, "clusters": []}, cols=60,
                              procs=[], height=10, core_only=True)
        self.assertIn("no CPU cores", frame)

    def test_core_and_cluster_gauges_share_the_same_chrome(self):
        # The per-core view used to hand-build its own bracket, which is how its
        # bar drifted to a different glyph than the dashboard's. Both must render
        # through bracket_gauge().
        shared = soltop.bracket_gauge(0.5, 10)
        self.assertTrue(shared.startswith("[") and shared.endswith("]"))
        # The dashboard's gauge line is exactly the shared chrome.
        self.assertEqual(soltop.hgauge("x", 0.5, 10)[1].strip(), shared)
        # ...and so is the per-core view's: a 0%-utilised core renders the
        # all-empty bracket verbatim.
        view = _core_view(n_e=1, n_p=0)
        view["clusters"][0]["per_core"][0]["pct"] = 0.0
        frame = soltop.render(view, cols=90, procs=[], core_only=True)
        self.assertIn(soltop.bracket_gauge(0.0, 90 - 10 - 32), frame)


class ProcGPUSamplerTests(unittest.TestCase):
    """step() diffs accumulated per-pid GPU time against the previous snapshot."""

    def _sampler(self, snapshots):
        ps = soltop.ProcGPUSampler()
        self._snaps = list(snapshots)
        return ps

    def _step_twice(self, snaps):
        """Run two steps over canned snapshots with 1s of simulated elapsed."""
        ps = soltop.ProcGPUSampler()
        with unittest.mock.patch.object(soltop_proc, "_gpu_client_totals",
                                        side_effect=snaps), \
                unittest.mock.patch.object(soltop_proc, "_attach_proc_stats",
                                           lambda rows: None):
            ps.step()                       # baseline
            ps.prev_time -= 1.0             # pretend 1s elapsed
            return ps.step()

    def test_a_newly_started_process_appears_on_the_next_sample(self):
        # `if pid in self.prev` skipped any pid absent from the previous
        # snapshot, so a process that launched after the baseline was invisible
        # for a whole interval. Its baseline is 0 -- it did not exist before.
        rows = self._step_twice([
            {1: ("old", 5_000_000_000)},
            {1: ("old", 5_000_000_000), 42: ("new", 500_000_000)},
        ])
        row = next(r for r in rows if r["pid"] == 42)
        self.assertAlmostEqual(row["gpu_ms_s"], 500.0, places=1)

    def test_first_snapshot_reports_nothing(self):
        # With no prior snapshot we know nothing about a pid's past, so a zero
        # baseline would credit its entire lifetime to one interval (WindowServer
        # would read ~17,000,000 ms/s).
        ps = soltop.ProcGPUSampler()
        with unittest.mock.patch.object(
                soltop_proc, "_gpu_client_totals",
                return_value={1: ("WindowServer", 17_000_000_000_000)}):
            self.assertEqual(ps.step(), [])

    def test_a_long_lived_process_never_reports_its_lifetime_total(self):
        huge = 17_000_000_000_000
        rows = self._step_twice([{1: ("WindowServer", huge)},
                                 {1: ("WindowServer", huge + 10_000_000)}])
        self.assertAlmostEqual(rows[0]["gpu_ms_s"], 10.0, places=1)

    def test_a_process_that_used_no_gpu_this_interval_is_not_listed(self):
        # Only processes that actually did GPU work in this interval belong in
        # the table; an idle GPU client would just be a row of zeroes.
        rows = self._step_twice([
            {1: ("busy", 5_000_000_000), 2: ("idle", 900)},
            {1: ("busy", 5_010_000_000), 2: ("idle", 900)},
        ])
        self.assertEqual([r["name"] for r in rows], ["busy"])



class ProcTableFormattingTests(unittest.TestCase):
    def test_fmt_bytes(self):
        self.assertEqual(soltop._fmt_bytes(None), "-")
        self.assertEqual(soltop._fmt_bytes(331 << 20), "331M")
        self.assertEqual(soltop._fmt_bytes(3 << 30), "3.0G")

    def test_fmt_bytes_rounds_before_picking_the_unit(self):
        # A naive `n >= 1<<30` threshold renders 1023.7 MiB as "1024M".
        self.assertEqual(soltop._fmt_bytes(int(1023.7 * (1 << 20))), "1.0G")
        self.assertEqual(soltop._fmt_bytes(1 << 40), "1.0T")

    def test_fmt_bytes_always_fits_the_mem_column(self):
        for n in (0, 1 << 20, 1023 << 20, 1 << 30, 900 << 30, 1 << 40):
            self.assertLessEqual(len(soltop._fmt_bytes(n)), 6, n)

    def test_table_shows_gpu_cpu_and_mem(self):
        rows = [{"pid": 42, "name": "app", "gpu_ms_s": 500.0,
                 "cpu_pct": 12.5, "rss_bytes": 331 << 20}]
        out = "\n".join(soltop.render_procs(rows))
        for want in ("GPU%", "CPU%", "MEM", "12.5", "331M", "app"):
            self.assertIn(want, out)

    def test_table_tolerates_missing_cpu_and_memory(self):
        # _attach_proc_stats is best-effort, so a row may carry neither.
        rows = [{"pid": 42, "name": "app", "gpu_ms_s": 1.0}]
        out = "\n".join(soltop.render_procs(rows))
        self.assertIn("app", out)


@needs_hardware
class SamplerLifecycleTests(unittest.TestCase):
    def test_failed_resubscribe_closes_instead_of_leaving_a_zombie(self):
        # _release() nulls every pointer, so if build_subscription() then raises,
        # the sampler was left closed=False with nothing behind it. The next
        # read() sailed past the closed guard, saw `not self.prev`, took the
        # recovery path again and SUCCEEDED -- resurrecting an object the caller
        # had been told was dead.
        s = soltop.Sampler()
        s.read(0.05)
        with unittest.mock.patch.object(soltop.IOR, "IOReportCreateSamples",
                                        lambda *a: None), \
                unittest.mock.patch.object(soltop, "build_subscription",
                                           side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                s.read(0.05)
        self.assertTrue(s.closed)
        with self.assertRaises(RuntimeError):
            s.read(0.05)

    def test_sampling_still_failing_after_resubscribe_closes_the_sampler(self):
        # Re-subscribing worked but sampling keeps failing: give up and close,
        # or the fresh subscription is leaked and the sampler claims to have
        # failed while still holding native resources.
        s = soltop.Sampler()
        s.read(0.05)
        with unittest.mock.patch.object(soltop.IOR, "IOReportCreateSamples",
                                        lambda *a: None):
            with self.assertRaises(RuntimeError):
                s.read(0.05)
        self.assertTrue(s.closed)
        self.assertIsNone(s.subscribed)
        with self.assertRaises(RuntimeError):
            s.read(0.05)

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

    def test_cluster_frequency_excludes_idle_residency(self):
        # This is powermetrics' "HW active frequency": the clock while a core is
        # actually running. Verified against `sudo powermetrics` -- for an
        # E-cluster at 73.21% active residency it prints 1920 MHz, which only the
        # active-weighted mean reproduces (folding idle in at the ladder floor
        # gives 1678 MHz).
        ladder = [1000, 2000, 3000]
        cores = [{"states": {"IDLE": 100, "V1P1": 20, "V2P0": 20}}]
        self.assertAlmostEqual(soltop.cluster_freq_mhz(cores, ladder), 2500)
        # A fully parked cluster has no active residency to report.
        parked = [{"states": {"IDLE": 100, "DOWN": 50}}]
        self.assertEqual(soltop.cluster_freq_mhz(parked, ladder), 0.0)
        # A fully pegged cluster sits at the top.
        pegged = [{"states": {"V2P0": 100}}]
        self.assertEqual(soltop.cluster_freq_mhz(pegged, ladder), 3000)

    def test_cpu_ladder_matches_powermetrics(self):
        # The CPU voltage-states table holds the PERIOD of each step, so
        # MHz = CPU_PERIOD_NUMERATOR / raw. These raw values are this M4 Pro's,
        # and the results are the exact ladders `sudo powermetrics` prints.
        e_raw = [64250, 46678, 36653, 31030, 27863, 25883, 25283]
        p_raw = [52012, 43343, 36408, 31386, 27863, 25051, 22850, 21167, 19859,
                 18897, 18083, 17448, 17013, 16701, 16400, 16205, 15968, 14840,
                 14524]

        def to_mhz(raws):
            return sorted(round(soltop.CPU_PERIOD_NUMERATOR / r) for r in raws)

        self.assertEqual(to_mhz(e_raw),
                         [1020, 1404, 1788, 2112, 2352, 2532, 2592])
        self.assertEqual(to_mhz(p_raw),
                         [1260, 1512, 1800, 2088, 2352, 2616, 2868, 3096, 3300,
                          3468, 3624, 3756, 3852, 3924, 3996, 4044, 4104, 4416,
                          4512])

    def test_m5_ladders_match_powermetrics(self):
        # Same numerator, a different chip: these raw values are an M5 Pro's
        # (voltage-states5 = S-cluster, voltage-states22/23 = P0/P1), and the
        # results are the exact ladders `sudo powermetrics` prints on it. The
        # period encoding and the numerator both carry across generations.
        s_raw = [50103, 40454, 33098, 28593, 25401, 22755, 20608, 19095, 17964,
                 17120, 16449, 15968, 15648, 15471, 15297, 15212, 15128, 14800,
                 14524, 14222]
        p_raw = [48761, 39863, 32899, 28444, 24711, 22110, 20227, 18703, 17731,
                 16908, 16205, 15693, 15297, 15212, 14962]

        def to_mhz(raws):
            return sorted(round(soltop.CPU_PERIOD_NUMERATOR / r) for r in raws)

        self.assertEqual(to_mhz(s_raw),
                         [1308, 1620, 1980, 2292, 2580, 2880, 3180, 3432, 3648,
                          3828, 3984, 4104, 4188, 4236, 4284, 4308, 4332, 4428,
                          4512, 4608])
        self.assertEqual(to_mhz(p_raw),
                         [1344, 1644, 1992, 2304, 2652, 2964, 3240, 3504, 3696,
                          3876, 4044, 4176, 4284, 4308, 4380])

    def test_ladder_binds_by_step_count_not_by_key_name(self):
        # The voltage-states NUMBERING is chip-specific: on an M4 Pro the P
        # ladder is voltage-states5, on an M5 Pro that same key holds the
        # S-cluster's and the P clusters live in 22/23. Binding by key name
        # therefore reads the wrong ladder on the next chip. Bind by the
        # cluster's own P-state count instead.
        tables = {
            "voltage-states5": ("period", [1308, 4608]),          # M5: S (2 steps)
            "voltage-states22": ("period", [1344, 2000, 4380]),   # M5: P0/P1 (3)
        }
        self.assertEqual(soltop.match_cpu_ladder(2, tables), [1308, 4608])
        self.assertEqual(soltop.match_cpu_ladder(3, tables), [1344, 2000, 4380])
        # A cluster whose ladder is absent gets no clock, rather than a wrong one.
        self.assertEqual(soltop.match_cpu_ladder(9, tables), [])
        self.assertEqual(soltop.match_cpu_ladder(0, tables), [])

    def test_gpu_cannot_bind_a_cpu_ladder(self):
        # The GPU's IOReport states are a fixed P1..P15 set on both chips, but
        # the real ladder is 15 steps on an M4 and 13 on an M5 -- so the state
        # count is NOT the ladder length and must not be matched against one.
        # Matching by length bound the M5's GPU to a 15-step CPU table (via its
        # kHz '-sram' twin) and reported 1644 MHz, above the 1620 MHz top of the
        # real GPU ladder.
        tables = {
            "voltage-states9": ("gpu", [338.0, 1620.0]),          # the real GPU
            "voltage-states22": ("period", [1344.0] * 15),        # CPU P ladder
            "voltage-states22-sram": ("sram", [1344.0] * 15),     # its kHz twin
        }
        self.assertEqual(soltop.match_gpu_ladder(tables), [338.0, 1620.0])
        # ... and the CPU still binds its own.
        self.assertEqual(soltop.match_cpu_ladder(15, tables), [1344.0] * 15)

    def test_ambiguous_cpu_ladders_report_no_clock(self):
        # A step count matching several tables is NORMAL -- a chip's two
        # performance clusters share a ladder (M4: voltage-states5 and 13, both
        # 1260..4512; M5: 22 and 23, both 1344..4380). Identical candidates are
        # no ambiguity, so the ladder is taken.
        agree = {"voltage-states5": ("period", [1260.0, 4512.0]),
                 "voltage-states13": ("period", [1260.0, 4512.0])}
        self.assertEqual(soltop.match_cpu_ladder(2, agree), [1260.0, 4512.0])

        # But candidates that DISAGREE mean the step count cannot say which is
        # this cluster's. Picking the lowest-numbered would render a guess as a
        # fact, so report no clock -- the same bargain the rest of the DVFS code
        # makes. No chip we have data for hits this; nothing promises none will.
        disagree = {"voltage-states5": ("period", [1260.0, 4512.0]),
                    "voltage-states13": ("period", [1000.0, 3000.0])}
        self.assertEqual(soltop.match_cpu_ladder(2, disagree), [])

    def test_sram_twins_are_excluded_from_matching(self):
        # Every '-sram' key duplicates its twin's ladder and so adds a spurious
        # candidate to every match -- which is how a CPU ladder came to be
        # offered to the GPU. A GPU table's sram twin is stored in Hz, so it even
        # decodes as a GPU table.
        tables = {"voltage-states9": ("gpu", [338.0, 1578.0]),
                  "voltage-states9-sram": ("gpu", [338.0, 1578.0]),
                  "voltage-states5": ("period", [1260.0, 4512.0]),
                  "voltage-states5-sram": ("sram", [1260.0, 4512.0])}
        # The twin must not make these look ambiguous...
        self.assertEqual(soltop.match_cpu_ladder(2, tables), [1260.0, 4512.0])
        self.assertEqual(soltop.match_gpu_ladder(tables), [338.0, 1578.0])

    def test_gpu_cap_excludes_the_impostor_tables_on_both_chips(self):
        # _GPU_MAX_MHZ cannot simply be raised "for headroom": the impostors sit
        # at 2364 on an M4 Pro and 2004/2472 on an M5, so the usable window is
        # roughly [1620, 2004). Raising it to 2400 was tried and immediately made
        # the M4's voltage-states8 (744..2364) outrank the real GPU table.
        self.assertGreater(soltop._GPU_MAX_MHZ, 1620.0)   # clears both real GPUs
        self.assertLess(soltop._GPU_MAX_MHZ, 2004.0)      # rejects every impostor

        m4 = {"voltage-states8": ("gpu", [744.0, 2364.0]),    # NOT the GPU
              "voltage-states9": ("gpu", [338.0, 1578.0])}    # the GPU
        self.assertEqual(soltop.match_gpu_ladder(m4), [338.0, 1578.0])

    def test_gpu_ignores_the_other_hz_tables(self):
        # An M5 Pro exposes several Hz tables beside the GPU's: 801..2004 and
        # 732..2472 are not GPU ladders. An Apple GPU clocks far below its CPU,
        # so the ceiling is what tells them apart -- picking the lowest-numbered
        # Hz table would have chosen voltage-states8 (732..2472).
        tables = {
            "voltage-states8": ("gpu", [732.0, 2472.0]),
            "voltage-states9": ("gpu", [338.0, 1620.0]),
        }
        self.assertEqual(soltop.match_gpu_ladder(tables), [338.0, 1620.0])
        self.assertEqual(soltop.match_gpu_ladder({}), [])

    def test_decode_ladder_identifies_the_encoding(self):
        # The kind must come from the ENCODING, not merely from landing in a
        # sane MHz range: a CPU table's kHz '-sram' twin decodes to a plausible
        # MHz ladder too, and lumping it in with the GPU's true-Hz tables is
        # what handed a CPU ladder to the GPU.
        self.assertIsNone(soltop._decode_ladder([1, 1, 1]))
        self.assertIsNone(soltop._decode_ladder([]))
        self.assertEqual(soltop._decode_ladder([338000000, 1620000000]),
                         ("gpu", [338.0, 1620.0]))        # true Hz -> GPU
        self.assertEqual(soltop._decode_ladder([1308000, 4608000]),
                         ("sram", [1308.0, 4608.0]))      # kHz -> a CPU twin
        kind, ladder = soltop._decode_ladder([50103, 14222])
        self.assertEqual((kind, [round(v) for v in ladder]), ("period", [1308, 4608]))

    def test_m5_core_names_group_into_the_real_clusters(self):
        # The core naming is chip-specific and the letters INVERT between chips:
        #   M4 Pro:  ECPU000..ECPU030   PCPU000..PCPU140
        #   M5 Pro:  PCPU0..PCPU4       MCPU00..MCPU14
        # so on an M5 'PCPU*' is the 5-core S-cluster and 'MCPU*' the 10
        # performance cores. Hardcoding the letters (or splitting on the leading
        # digit regardless) turned PCPU0..PCPU4 into five one-core clusters and
        # dropped MCPU* into "CPU other".
        def cores(names, nsteps):
            # Give each core a ladder-length-worth of P-states, so the tier can
            # be ranked by its ceiling (see _tier_labels).
            states = {"IDLE": 1}
            states.update({f"V{i}P{nsteps - 1 - i}": 1 for i in range(nsteps)})
            return [{"name": n, "active": 0.0, "states": dict(states)} for n in names]

        saved = soltop_dvfs.DVFS
        try:
            # M5 Pro: 1-digit names are core indices within ONE cluster, and the
            # 20-step tier (4608 MHz) outranks the 15-step one (4380) -> S over P.
            soltop_dvfs.set_tables({"voltage-states5": ("period", [1308.0] + [4608.0] * 19),
                           "voltage-states22": ("period", [1344.0] + [4380.0] * 14)})
            m5 = soltop.group_clusters(cores([f"PCPU{i}" for i in range(5)], 20) +
                                       cores([f"MCPU{c}{i}" for c in (0, 1)
                                              for i in range(5)], 15))
            self.assertEqual([(c["key"], len(c["cores"])) for c in m5],
                             [("S", 5), ("P0", 5), ("P1", 5)])

            # M4 Pro: 3-digit names carry a leading CLUSTER index, and the slow
            # tier (2592) is far below the fast one (4512) -> a real E-cluster.
            soltop_dvfs.set_tables({"voltage-states1": ("period", [1020.0] + [2592.0] * 6),
                           "voltage-states5": ("period", [1260.0] + [4512.0] * 18)})
            m4 = soltop.group_clusters(cores([f"ECPU0{i}0" for i in range(4)], 7) +
                                       cores([f"PCPU{c}{i}0" for c in (0, 1)
                                              for i in range(5)], 19))
            self.assertEqual([(c["key"], len(c["cores"])) for c in m4],
                             [("E", 4), ("P0", 5), ("P1", 5)])

            # An unrecognised name is still shown, never dropped.
            self.assertEqual(
                [c["key"] for c in soltop.group_clusters(cores(["WEIRD"], 3))], ["?"])
        finally:
            soltop_dvfs.set_tables(saved)

    def test_tier_labels_come_from_the_ladder_not_the_name(self):
        # 'PCPU' means Performance on an M4 and Super on an M5, so the letter in
        # the core name cannot name the tier. Rank by ladder ceiling instead.
        #
        # M4 Pro: the slow tier is 57% of the fast one -> an efficiency cluster.
        self.assertEqual(soltop._tier_labels({"E": 2592.0, "P": 4512.0}),
                         {"E": "E", "P": "P"})
        # M5 Pro: the slow tier is 95% of the fast one -> both are performance
        # class, so the faster one is Apple's "Super" cluster.
        self.assertEqual(soltop._tier_labels({"P": 4608.0, "M": 4380.0}),
                         {"P": "S", "M": "P"})
        # A single tier is just P; an unknown ladder keeps its raw letter rather
        # than having a tier invented for it.
        self.assertEqual(soltop._tier_labels({"P": 4512.0}), {"P": "P"})
        self.assertEqual(soltop._tier_labels({"X": 0.0}), {"X": "X"})

    def test_nsteps_counts_the_non_idle_states(self):
        # The ladder length is the number of non-idle P-states IOReport names.
        core = [{"states": {"DOWN": 1, "IDLE": 2, "V0P2": 3, "V1P1": 4, "V2P0": 5}}]
        self.assertEqual(soltop._nsteps(core), 3)
        self.assertEqual(soltop._nsteps([{"states": {}}]), 0)
        self.assertEqual(soltop._nsteps([]), 0)
        # The widest count wins: a core reporting a short list must not bind a
        # shorter ladder and yield a plausible wrong clock.
        self.assertEqual(soltop._nsteps([{"states": {"V0P1": 1}}] + core), 3)

    def test_power_rejects_a_channel_counting_in_the_wrong_unit(self):
        # Not every Energy Model channel counts in mJ. 'GPU Energy' is in uJ:
        # read as mJ it comes out as 272 kW on an M4 Pro and 2833 W on an M5.
        # soltop uses 'GPU' today, so this is latent -- but a chip that drops
        # 'GPU' and keeps only 'GPU Energy' would render a four-digit wattage
        # with total confidence. Above any plausible SoC budget, report nothing.
        self.assertGreater(soltop.POWER_SANE_MAX_MW, 100_000.0)   # a real SoC fits
        self.assertLess(soltop.POWER_SANE_MAX_MW, 1_000_000.0)    # 272 kW does not

        # The real captured values: an M4 Pro's implausible reading is rejected,
        # while both chips' genuine CPU/GPU readings are kept.
        for mw, ok in ((272_731_404.0, False),   # M4 Pro 'GPU Energy' (uJ)
                       (2_833_000.0, False),     # M5 Pro 'GPU Energy' (uJ)
                       (1195.0, True),           # M4 Pro 'CPU Energy'
                       (786.0, True),            # M5 Pro 'CPU Energy'
                       (3.0, True)):             # M5 Pro 'GPU'
            self.assertEqual(mw <= soltop.POWER_SANE_MAX_MW, ok, mw)

    @needs_hardware
    def test_power_filter_engages_on_the_real_read_path(self):
        # Not just the constant -- the filter has to be applied where the energy
        # delta is actually turned into a wattage. Aim a label at 'GPU Energy'
        # (which counts in uJ) and drive a real Sampler.read: the GPU must report
        # nothing rather than the ~272 kW that channel reads as.
        sampler = soltop.Sampler()
        saved = soltop_sampler.ENERGY_KEYS
        try:
            soltop_sampler.ENERGY_KEYS = {"CPU Energy": "CPU", "GPU Energy": "GPU"}
            power = sampler.read(0.3)["power"]
        finally:
            soltop_sampler.ENERGY_KEYS = saved
            sampler.close()

        # 'GPU Energy' is real and non-zero on this machine, but implausible as
        # power -- so it is dropped rather than rendered.
        self.assertEqual(power["GPU"], 0.0)
        # ... while the sane channel still reports, and the SoC total (a sum of
        # the survivors) cannot be poisoned by the rejected one.
        self.assertGreater(power["CPU"], 0.0)
        self.assertLessEqual(power["SoC"], soltop.POWER_SANE_MAX_MW)

    def _export_view(self):
        """A view with a live cluster and a parked one (the M5 Pro's P1)."""
        return {
            "gpu_pct": 29.4, "gpu_mhz": 618.0, "gpu_channels": [],
            "clusters": [
                {"key": "P0", "label": "CPU P0-cluster", "count": 5,
                 "avg": 90.0, "mhz": 4380.0, "cores": [], "per_core": []},
                {"key": "P1", "label": "CPU P1-cluster", "count": 5,
                 "avg": 0.0, "mhz": 0.0, "cores": [], "per_core": []},
            ],
            "power": {"CPU": 1360.0, "GPU": 152.4, "SoC": 2110.0},
        }

    def test_export_never_reports_an_unknown_clock_as_zero(self):
        # The whole point of soltop's degradation story, carried into the export:
        # a parked cluster (macOS powers whole clusters down) and unreadable
        # silicon both have NO clock. Exporting that as 0 would be a lie that
        # averages cleanly -- a Grafana panel would quietly drag towards zero.
        snap = soltop.snapshot(self._export_view(), timestamp=1234.5)

        live, parked = snap["cpu_clusters"]
        self.assertEqual(live["frequency_mhz"], 4380)
        self.assertIsNone(parked["frequency_mhz"])     # null, NOT 0
        self.assertTrue(parked["parked"])
        self.assertFalse(live["parked"])

        # JSON: null.
        self.assertIn('"frequency_mhz":null', soltop.to_json(snap))

        # CSV: an empty field, not a 0.
        cols = soltop._csv_columns(snap)
        row = soltop.to_csv_row(snap).split(",")
        got = dict(zip(cols, row))
        self.assertEqual(got["cpu_P0_frequency_mhz"], "4380")
        self.assertEqual(got["cpu_P1_frequency_mhz"], "")

        # Prometheus: the series is OMITTED. An absent series is honest; a zeroed
        # one is not.
        text = soltop.to_prometheus(snap)
        self.assertIn('soltop_cpu_frequency_mhz{cluster="P0"} 4380', text)
        self.assertNotIn('soltop_cpu_frequency_mhz{cluster="P1"}', text)
        # ... but its utilization (a real 0%) IS exported.
        self.assertIn('soltop_cpu_utilization_percent{cluster="P1"} 0.0', text)

    def test_prometheus_output_is_well_formed(self):
        text = soltop.to_prometheus(soltop.snapshot(self._export_view(), 1234.5))
        lines = [l for l in text.splitlines() if l]
        # Every metric carries HELP and TYPE before its samples.
        for name in ("soltop_gpu_utilization_percent", "soltop_cpu_cores",
                     "soltop_power_milliwatts"):
            self.assertIn(f"# HELP {name} ", text)
            self.assertIn(f"# TYPE {name} gauge", text)
        # Labels are quoted, and no sample line is left with a bare trailing brace.
        for line in lines:
            if line.startswith("#"):
                continue
            self.assertRegex(line, r'^[a-z_]+(\{[a-z_]+="[^"]*"(,[a-z_]+="[^"]*")*\})? '
                                   r'-?[0-9.]+$', line)
        self.assertTrue(text.endswith("\n"))

    def test_export_survives_a_failure_to_read_a_decorative_field(self):
        # An exporter is a long-lived process. Failing to read the machine's name
        # or its thermal state must not take the whole metric stream down --
        # those are labels, not measurements.
        with unittest.mock.patch.object(soltop_formats, "machine_name",
                                        side_effect=Exception("boom")), \
                unittest.mock.patch.object(soltop_formats, "thermal_state",
                                           side_effect=Exception("boom")):
            snap = soltop.snapshot(self._export_view(), timestamp=1234.5)

        self.assertIsNone(snap["machine"])
        self.assertIsNone(snap["thermal"])
        # The measurements still made it out.
        self.assertEqual(snap["gpu"]["frequency_mhz"], 618)
        self.assertIn('soltop_gpu_frequency_mhz 618', soltop.to_prometheus(snap))
        self.assertIn('machine="unknown"', soltop.to_prometheus(snap))

    def test_soc_temp_is_not_a_gpu_temperature(self):
        # This machine exposes no GPU-specific sensor, and the die sensors barely
        # notice the GPU: pinning it with a Metal kernel moves them ~+1 C, while
        # pinning the CPU moves the same sensors +13 C. Other tools look for a
        # sensor whose NAME contains "GPU" and silently report 0.0 when there is
        # none. soltop reports the SoC die and says so -- in the label, the JSON
        # key, and the Prometheus HELP text.
        snap = soltop.snapshot(self._export_view(), timestamp=1234.5)
        self.assertIn("soc_temp_celsius", snap)
        self.assertNotIn("gpu_temp", soltop.to_json(snap))
        text = soltop.to_prometheus(snap)
        if "soltop_soc_temperature_celsius" in text:
            self.assertIn("NOT a GPU temperature", text)

    def test_soc_temp_is_absent_rather_than_zero_when_unreadable(self):
        # A machine with no readable die sensor must report NOTHING, not 0 C --
        # a zero would look like a very cold chip and would average cleanly.
        with unittest.mock.patch.object(soltop_temps, "die_temps", return_value=[]):
            self.assertEqual(soltop_temps.soc_temp(), {})

        view = dict(self._export_view())
        view["soc_temp"] = {}
        snap = soltop.snapshot(view, timestamp=1234.5)
        self.assertIsNone(snap["soc_temp_celsius"])
        self.assertIn('"soc_temp_celsius":null', soltop.to_json(snap))
        # Prometheus omits the series entirely rather than exporting a 0.
        self.assertNotIn("soltop_soc_temperature_celsius",
                         soltop.to_prometheus(snap))
        # CSV leaves the fields empty.
        cols = soltop._csv_columns(snap)
        got = dict(zip(cols, soltop.to_csv_row(snap).split(",")))
        self.assertEqual(got["soc_temp_max_celsius"], "")

    def test_die_sensors_exclude_the_non_die_ones(self):
        # 'PMU tcal' sits pinned at 51.82 C under any load (a calibration
        # constant, not a temperature), and the battery and NAND are not the die.
        self.assertTrue(soltop_temps._DIE_RE.match("PMU tdie1"))
        self.assertTrue(soltop_temps._DIE_RE.match("PMU tdie14"))
        for name in ("PMU tcal", "gas gauge battery", "NAND CH0 temp", "GPU"):
            self.assertIsNone(soltop_temps._DIE_RE.match(name), name)

    def test_serve_address_parsing(self):
        # A bare port must NOT bind every interface: exporting hardware telemetry
        # to the network should be a deliberate act.
        self.assertEqual(soltop._parse_addr("9101"), ("127.0.0.1", 9101))
        self.assertEqual(soltop._parse_addr(":9101"), ("127.0.0.1", 9101))
        self.assertEqual(soltop._parse_addr("0.0.0.0:9101"), ("0.0.0.0", 9101))
        for bad in ("", "nope", "9101:", "0", "65536", "1.2.3.4"):
            with self.assertRaises(ValueError, msg=bad):
                soltop._parse_addr(bad)

    def test_unknown_silicon_hides_the_clock_instead_of_faking_one(self):
        # The whole point of binding ladders by shape rather than by name: on a
        # chip whose tables we cannot read, soltop must show NO clock. A wrong
        # number is worse than a missing one -- an M5 Pro would have rendered the
        # S-cluster's ladder as the P-cluster's and nobody would have noticed.
        sub = "CPU Core Performance States"
        cores = [{"name": "XCPU00", "subgroup": sub, "active": 0.5,
                  "states": {"IDLE": 50, "V0P1": 25, "V1P0": 25}}]

        saved = soltop_dvfs.DVFS
        try:
            # No tables at all (a future chip that renames voltage-states*).
            soltop_dvfs.set_tables({})
            v = soltop.organize({"gpu": [], "cpu": cores})
            c = v["clusters"][0]
            self.assertEqual(c["mhz"], 0.0)
            self.assertEqual(soltop._freq_txt(c["mhz"]), "")
            # Utilization still works -- it does not depend on the DVFS tables.
            self.assertEqual(c["avg"], 50.0)

            # Tables exist but none has this cluster's step count: still no clock.
            soltop_dvfs.set_tables({"voltage-states5": ("period", [1000.0] * 19)})
            c = soltop.organize({"gpu": [], "cpu": cores})["clusters"][0]
            self.assertEqual(c["mhz"], 0.0)
            self.assertEqual(c["avg"], 50.0)
        finally:
            soltop_dvfs.set_tables(saved)

    def test_m5_pro_18_core_variant(self):
        # The 18-core M5 Pro is 6 Super + 12 Performance. The layout is derived
        # from the names, so this needs no code change -- pin it so it stays that
        # way: 6 single-digit names stay ONE cluster, 12 two-digit names split on
        # the leading digit.
        def cores(names, nsteps):
            states = {"IDLE": 1}
            states.update({f"V{i}P{nsteps - 1 - i}": 1 for i in range(nsteps)})
            return [{"name": n, "subgroup": "CPU Core Performance States",
                     "active": 0.0, "states": dict(states)} for n in names]

        saved = soltop_dvfs.DVFS
        try:
            soltop_dvfs.set_tables({"voltage-states5": ("period", [1308.0] + [4608.0] * 19),
                           "voltage-states22": ("period", [1344.0] + [4380.0] * 14)})
            got = soltop.group_clusters(
                cores([f"PCPU{i}" for i in range(6)], 20) +
                cores([f"MCPU{c}{i}" for c in (0, 1) for i in range(6)], 15))
            self.assertEqual([(c["key"], len(c["cores"])) for c in got],
                             [("S", 6), ("P0", 6), ("P1", 6)])
        finally:
            soltop_dvfs.set_tables(saved)

    def test_m5_pro_topology_end_to_end(self):
        # An M5 Pro as powermetrics reports it: an S-cluster and TWO P-clusters,
        # no E-cluster at all, with P1 fully powered down. Each cluster must bind
        # its own ladder, and the parked one must report no clock rather than a
        # made-up one.
        def states(nsteps, top_res, idle):
            s = dict(idle)
            s.update({f"V{i}P{nsteps - 1 - i}": 0 for i in range(nsteps)})
            s[f"V{nsteps - 1}P0"] = top_res
            return s

        # The real M5 Pro channel names: PCPU0..4 are the S-cluster, MCPU00..14
        # are the two performance clusters.
        sub = "CPU Core Performance States"
        raw = {"gpu": [], "cpu":
               [{"name": f"PCPU{i}", "subgroup": sub, "active": 0.06,
                 "states": states(20, 6, {"DOWN": 74, "IDLE": 20})} for i in range(5)] +
               [{"name": f"MCPU0{i}", "subgroup": sub, "active": 0.99,
                 "states": states(15, 99, {"IDLE": 1})} for i in range(5)] +
               [{"name": f"MCPU1{i}", "subgroup": sub, "active": 0.0,
                 "states": states(15, 0, {"DOWN": 100})} for i in range(5)]}
        saved = soltop_dvfs.DVFS
        soltop_dvfs.set_tables({
            "voltage-states5": ("period", [1308.0] + [4608.0] * 19),    # 20 steps
            "voltage-states22": ("period", [1344.0] + [4380.0] * 14),   # 15 steps
        })
        try:
            got = {c["key"]: c for c in soltop.organize(raw)["clusters"]}
        finally:
            soltop_dvfs.set_tables(saved)

        # The Super cluster is labelled S, not P, even though its cores are
        # named PCPU* -- the tier comes from the ladder ceiling (4608 > 4380).
        self.assertEqual(sorted(got), ["P0", "P1", "S"])
        self.assertEqual(got["S"]["count"], 5)       # one cluster of 5
        self.assertEqual(got["S"]["mhz"], 4608.0)    # 20-step ladder, top step
        self.assertEqual(got["P0"]["mhz"], 4380.0)   # 15-step ladder, top step
        # Parked: no active residency to weight, so no clock is claimed.
        self.assertEqual(got["P1"]["avg"], 0.0)
        self.assertEqual(got["P1"]["mhz"], 0.0)
        self.assertEqual(soltop._freq_txt(got["P1"]["mhz"]), "")

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

    def test_cluster_freq_reads_the_ladder_by_index(self):
        cores = [{"states": {"V1P1": 10}}]      # V=1 -> ladder index 1
        table = {"values": [1000, 2000, 3000], "unit": "MHz"}
        self.assertEqual(soltop.cluster_freq_mhz(cores, table), 2000)
        self.assertEqual(soltop.cluster_freq_mhz(cores, []), 0.0)

    def test_freq_txt(self):
        self.assertEqual(soltop._freq_txt(1398.0), "@ 1398 MHz")
        self.assertEqual(soltop._freq_txt(0.0), "")

    def test_version(self):
        self.assertEqual(soltop.__version__, "0.10.1")

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
            "power_avg": {}, "power_peak": {},
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
