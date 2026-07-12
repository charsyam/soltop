import io
import subprocess
import sys
import time
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
                soc_hist=None, process_only=False, single_sample=False,
                core_only=False):
            seen.append(process_only)
            return real_render(view, cols, gpu_hist, procs, height, soc_hist,
                               process_only, single_sample, core_only)

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

    def _run_live_views(self, keys):
        """Run live() against fakes, returning which view each frame drew."""
        seen = []
        real_render = soltop.render

        def spy(view, cols=80, gpu_hist=None, procs=None, height=None,
                soc_hist=None, process_only=False, single_sample=False,
                core_only=False):
            seen.append("proc" if process_only
                        else "core" if core_only else "dash")
            return real_render(view, cols, gpu_hist, procs, height, soc_hist,
                               process_only, single_sample, core_only)

        with unittest.mock.patch.object(soltop, "Sampler", _FakeSampler), \
                unittest.mock.patch.object(soltop, "ProcGPUSampler", _FakeProcSampler), \
                unittest.mock.patch.object(soltop, "KeyReader", lambda s: _FakeKeys(keys)), \
                unittest.mock.patch.object(soltop.time, "sleep", lambda s: None), \
                unittest.mock.patch.object(soltop, "render", spy), \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            soltop.live(interval=0.01)
        return seen

    def test_c_toggles_the_per_core_view_and_back(self):
        self.assertEqual(self._run_live_views(["", "c", "", "c", "", "q"]),
                         ["dash", "core", "core", "dash", "dash"])

    def test_core_and_process_views_are_mutually_exclusive(self):
        # 'c' from the process view switches to cores rather than stacking, and
        # 'p' from the core view switches back.
        # Keys are handled before each frame is drawn, and 'q' returns without
        # drawing, so four reads produce three frames.
        self.assertEqual(self._run_live_views(["p", "c", "p", "q"]),
                         ["proc", "core", "proc"])


class LiveRecoveryTests(unittest.TestCase):
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
                unittest.mock.patch.object(soltop, "KeyReader", lambda s: Keys()), \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            soltop.live(interval=0.01)      # must not raise
        self.assertGreater(calls["n"], 2, "should have sampled past the failure")


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
                unittest.mock.patch.object(soltop, "KeyReader", lambda s: Keys()), \
                unittest.mock.patch.object(soltop.time, "sleep", lambda s: None), \
                unittest.mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(RuntimeError):
                soltop.live(interval=0.01)
        self.assertLessEqual(calls["n"], soltop.LIVE_MAX_RETRIES + 1)

    def test_main_reports_a_sampler_failure_without_a_traceback(self):
        def boom(*a, **k):
            raise RuntimeError("subscription failed")

        err = io.StringIO()
        with unittest.mock.patch.object(soltop, "Sampler", boom), \
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
        with unittest.mock.patch.object(soltop, "_gpu_client_totals",
                                        side_effect=snaps), \
                unittest.mock.patch.object(soltop, "_attach_proc_stats",
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
                soltop, "_gpu_client_totals",
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
        self.assertEqual(soltop.__version__, "0.7.1")

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
