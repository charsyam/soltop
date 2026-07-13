"""Command line entry point."""
import argparse
import sys

from . import __version__
from .core.process import ProcGPUSampler
from .core.sampler import Sampler
from .core.view import organize
from .exporter.serve import serve, stream
from .ui import live, render, term_size


def main():
    import argparse

    p = argparse.ArgumentParser(prog="soltop", description="Apple Silicon GPU/CPU usage monitor")
    def positive_float(value):
        import math
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise argparse.ArgumentTypeError("must be a finite number greater than 0")
        return parsed

    def nonnegative_int(value):
        parsed = int(value)
        if parsed < 0:
            raise argparse.ArgumentTypeError("must be 0 or greater")
        return parsed

    p.add_argument("-i", "--interval", type=positive_float, default=1.0,
                   help="sampling interval (seconds)")
    p.add_argument("-c", "--cols", type=nonnegative_int, default=0,
                   help="terminal columns to use (0 = auto-fit)")
    p.add_argument("--once", action="store_true", help="print once and exit")
    p.add_argument("--json", action="store_true",
                   help="emit one JSON object per sample (JSONL) instead of the TUI")
    p.add_argument("--csv", action="store_true",
                   help="emit CSV (a header, then one row per sample)")
    p.add_argument("--serve", metavar="[ADDR:]PORT",
                   help="serve Prometheus metrics at /metrics (e.g. 9101, :9101, "
                        "127.0.0.1:9101). Binds localhost unless an address is given")
    p.add_argument("--version", action="version", version=f"soltop {__version__}")
    args = p.parse_args()
    cols_override = args.cols or None

    if args.json and args.csv:
        p.error("--json and --csv are mutually exclusive")
    if args.serve and (args.json or args.csv):
        p.error("--serve cannot be combined with --json/--csv")

    # IOReport can refuse to subscribe (unsupported machine, or it simply keeps
    # failing). Report that as a message, not a Python traceback.
    try:
        if args.serve:
            return serve(args.serve, args.interval)
        if args.json or args.csv:
            return stream(args.interval, as_csv=args.csv, once=args.once)
        if args.once:
            sampler = Sampler()
            try:
                try:
                    proc_sampler = ProcGPUSampler()
                    proc_sampler.step()  # establish the process baseline before the wait
                except Exception:
                    proc_sampler = None
                view = organize(sampler.read(args.interval))
                try:
                    procs = proc_sampler.step() if proc_sampler else []
                except Exception:
                    procs = []
                tcols, rows = term_size()
                # One sample: no history worth graphing, and no meaningful avg/peak.
                print(render(view, cols_override or tcols, gpu_hist=None, procs=procs,
                             height=rows, soc_hist=None, single_sample=True))
            finally:
                sampler.close()
        else:
            live(args.interval, cols_override)
    except RuntimeError as e:
        import sys
        print(f"soltop: {e}", file=sys.stderr)
        return 1
    return 0
