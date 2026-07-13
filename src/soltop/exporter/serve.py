"""Stream to stdout (JSONL/CSV), or serve Prometheus metrics over HTTP."""
import sys
import time

from .. import __version__
from ..core.sampler import Sampler
from ..core.view import organize
from .formats import _csv_columns, snapshot, to_csv_row, to_json, to_prometheus


def _sample_snapshots(interval):
    """Yield a snapshot per interval, forever. Shared by stream() and serve()."""
    sampler = Sampler()
    try:
        while True:
            view = organize(sampler.read(interval))
            yield snapshot(view, time.time())
    finally:
        sampler.close()


def stream(interval=1.0, as_csv=False, once=False):
    """Emit JSONL (or CSV) on stdout, one record per sample.

    Line-buffered and flushed per record, so it pipes into `jq`, a log shipper,
    or `tee` without the consumer waiting on a full buffer.
    """
    header_written = False
    try:
        for snap in _sample_snapshots(interval):
            if as_csv:
                if not header_written:
                    print(",".join(_csv_columns(snap)), flush=True)
                    header_written = True
                print(to_csv_row(snap), flush=True)
            else:
                print(to_json(snap), flush=True)
            if once:
                break
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        # `soltop --json | head -5` closes the pipe on us; that is not an error.
        pass
    return 0


def _parse_addr(spec, default_host="127.0.0.1"):
    """'9101' | ':9101' | '0.0.0.0:9101' -> (host, port). Raises on nonsense."""
    spec = str(spec)
    host, _, port = spec.rpartition(":")
    if not port.isdigit():
        raise ValueError(f"not a port: {spec!r}")
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    # Bare '9101' or ':9101' -> loopback. Exporting hardware telemetry to every
    # interface should be a deliberate act, not the default.
    return (host or default_host, port)


def serve(addr, interval=1.0):
    """Serve Prometheus metrics at /metrics.

    A background thread samples continuously and publishes the latest snapshot;
    scrapes read that rather than each triggering their own `interval`-long
    sample. So a scrape returns immediately, and N scrapers cost no more than 1.
    """
    import http.server
    import threading

    try:
        host, port = _parse_addr(addr)
    except ValueError as e:
        import sys
        print(f"soltop: --serve: {e}", file=sys.stderr)
        return 2

    latest = {"snap": None, "error": None}

    def poll():
        try:
            for snap in _sample_snapshots(interval):
                latest["snap"] = snap
        except Exception as e:                  # the sampler died; say so on /metrics
            latest["error"] = str(e)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] not in ("/metrics", "/"):
                self.send_error(404)
                return
            snap, err = latest["snap"], latest["error"]
            if err:
                self.send_error(503, f"sampler failed: {err}")
                return
            if snap is None:                    # first sample not in yet
                self.send_error(503, "warming up")
                return
            body = to_prometheus(snap).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):              # don't spam stdout per scrape
            pass

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()

    httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    import sys
    print(f"soltop {__version__}: serving metrics on http://{host}:{port}/metrics",
          file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
