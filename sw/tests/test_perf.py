from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import perf
from fft_model import FFTConfig, fixed_fft, generate_samples, pack_samples
from perf_reports import HALF, MOD, RECORD, ReportStream, analyze, decode, distribution
from test_host import FakePort


def report(seq, start):
    return RECORD.pack(b"\xa5\x5a", seq % MOD, start % MOD,
                       (start + 10) % MOD, (start + 30) % MOD, (start + 50) % MOD)


def capture(raws, mode="throughput"):
    return {"schema": perf.SCHEMA, "mode": mode,
            "config": {"port": "fake", "baudrate": 115200, "fclk": 1000, "N": 8,
                       "width": 16, "integer_width": 2, "round": 1,
                       "saturation": 1, "radix_out_scale": 1},
            "raw_reports_hex": [r.hex() for r in raws], "duration_seconds": 2,
            "verify_enabled": False, "error": None}


class ReportTests(unittest.TestCase):
    def test_exact_metrics_and_units(self):
        self.assertEqual(report(1, 100).hex(), "a55a01000000640000006e0000008200000096000000")
        c = capture([report(1, 100), report(2, 200), report(3, 300)])
        self.assertEqual(perf.calculate(c), 0)
        self.assertEqual(c["reports"][0]["cycles"], {
            "core_gap": 20, "export": 20, "service": 40, "latency": 50,
            "input_span": 10, "output_span": 20})
        self.assertEqual(c["reports"][0]["us"]["latency"], 50000)
        self.assertEqual(c["summary"]["period"]["mean"], 100)
        self.assertEqual(c["throughput"]["frames_per_s"], 10)
        self.assertEqual(c["throughput"]["samples_per_s"], 80)
        self.assertEqual(c["throughput"]["samples_per_cycle"], .08)
        self.assertEqual(c["counters"]["frames_accounted"], 3)
        self.assertEqual(c["counters"]["report_rate_hz"], 1.5)

    def test_weighted_skips_not_mean_of_means(self):
        c = capture([report(1, 0), report(3, 200), report(6, 800)])
        perf.calculate(c)
        self.assertEqual([p["period"] for p in c["pairs"]], [100, 200])
        self.assertEqual(c["counters"]["frames_skipped"], 3)
        self.assertEqual(c["counters"]["frames_accounted"], 6)
        self.assertEqual(c["throughput"]["samples_per_s"], 8 * 1000 * 5 / 800)

    def test_wraps(self):
        c = capture([report(MOD-1, MOD-40), report(0, 60), report(1, 160)])
        self.assertEqual(perf.calculate(c), 0)
        self.assertEqual(c["counters"]["seq_wraps"], 1)
        self.assertEqual(c["reports"][0]["cycles"]["latency"], 50)
        self.assertEqual(c["pairs"][0]["period"], 100)

    def test_invalid_order_duplicate_and_ambiguous_pair(self):
        raw = RECORD.pack(b"\xa5\x5a", 1, 100, 90, 120, 140)
        with self.assertRaises(ValueError):
            decode(raw)
        for raws in ([report(1, 0), report(1, 100)], [report(1, 0), report(2, HALF+100)]):
            c = capture(raws)
            self.assertEqual(perf.calculate(c), 1)
            self.assertEqual(c["pairs"], [])

    def test_resync_baseline_does_not_bridge_gap(self):
        c = capture([report(1, 100), report(2, 200), report(40, 4000), report(41, 4100)])
        c["report_segments"] = [0, 0, 1, 1]
        perf.calculate(c)
        self.assertEqual(len(c["pairs"]), 2)
        self.assertEqual(c["counters"]["frames_accounted"], 4)

    def test_statistics_nearest_rank_and_population_std(self):
        d = distribution([1, 2, 3, 4])
        self.assertEqual((d["p50"], d["p95"], d["p99"]), (2, 4, 4))
        self.assertAlmostEqual(d["std"], 1.118033988749895, places=12)
        self.assertIsNone(distribution([]))

    def test_sync_partial_and_payload_marker(self):
        # Valid little-endian seq contains the same sync bytes as the header.
        records = [report(0x5aa5 + i, 100*i) for i in range(4)]
        stream = ReportStream()
        output = []
        wire = b"garbage\xa5" + b"".join(records)
        for byte in wire:
            output += stream.feed(bytes([byte]))
        stream.finish()
        self.assertEqual([r for r, _ in output], records)
        self.assertGreater(stream.discarded["garbage_bytes"], 0)
        self.assertFalse(stream.buffer)

    def test_one_byte_shift_and_reacquisition(self):
        records = [report(i, 100*i) for i in range(1, 7)]
        stream = ReportStream()
        output = stream.feed(b"".join(records[:2]) + records[2][:10] + records[2][11:] + b"".join(records[3:]))
        self.assertEqual([r["seq"] for _, r in output], [1, 2, 4, 5, 6])
        self.assertEqual([r["segment"] for _, r in output], [0, 0, 1, 1, 1])
        stream.feed(report(7, 700)[:11])
        stream.finish()
        self.assertEqual(stream.discarded["trailing_or_unconfirmed_bytes"], 11)

    def test_requires_two_for_initial_lock(self):
        stream = ReportStream()
        self.assertEqual(stream.feed(report(1, 0)), [])
        self.assertEqual(len(stream.feed(report(2, 100))), 2)


class PerfCLITests(unittest.TestCase):
    def invoke(self, args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return perf.main(args)

    def test_replay_same_metrics_without_port(self):
        with tempfile.TemporaryDirectory() as folder:
            original, replay = Path(folder)/"original.json", Path(folder)/"replay.json"
            c = capture([report(1, 100), report(2, 200), report(3, 300)])
            perf.calculate(c)
            perf.save(original, c)
            with patch.object(perf, "open_port") as opened:
                self.assertEqual(self.invoke(["--replay", str(original), "--capture", str(replay)]), 0)
                opened.assert_not_called()
            loaded = json.loads(replay.read_text())
            for key in ("summary", "summary_us", "throughput", "counters", "pairs"):
                self.assertEqual(c[key], loaded[key])
            json.dumps(loaded, allow_nan=False)

    def test_normal_verification_and_no_verify(self):
        config = FFTConfig(n=8)
        samples = generate_samples(config, random.Random(1))
        response = pack_samples(fixed_fft(samples, config)[0], config) + report(1, 100)
        for no_verify in (False, True):
            port = FakePort(response)
            port.close = lambda: None
            with patch.object(perf, "open_port", return_value=port):
                self.assertEqual(self.invoke(["--port", "fake", "--N", "8"] + (["--no-verify"] if no_verify else [])), 0)
            self.assertEqual(bytes(port.sent), pack_samples(samples, config))
        corrupted = bytearray(response)
        corrupted[0] ^= 1
        for extra, expected in (([], 1), (["--no-verify"], 0)):
            port = FakePort(corrupted)
            port.close = lambda: None
            with patch.object(perf, "open_port", return_value=port):
                self.assertEqual(self.invoke(["--port", "fake", "--N", "8"] + extra), expected)

    def test_partial_timeout_and_empty_stream(self):
        for data in (b"", report(1, 0)[:10]):
            port = FakePort(data)
            c = capture([])
            c["report_segments"] = []
            times = iter(i / 10 for i in range(100))
            with self.assertRaises(perf.MeasurementError):
                perf.receive_reports(port, 3, .5, c, clock=lambda: next(times))
            self.assertEqual(c["raw_reports_hex"], [])

    def test_disconnect_capture_and_exit_codes(self):
        port = FakePort(b"")
        port.read = lambda _: (_ for _ in ()).throw(OSError("disconnected"))
        port.close = lambda: None
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"failure.json"
            with patch.object(perf, "open_port", return_value=port):
                self.assertEqual(self.invoke(["--port", "fake", "--mode", "throughput", "--capture", str(path)]), 1)
            self.assertIn("disconnected", json.loads(path.read_text())["error"])
            self.assertEqual(self.invoke(["--replay", str(path)]), 1)
            self.assertEqual(self.invoke(["--replay", str(path)+"missing"]), 2)
        with patch.object(perf, "open_port", side_effect=OSError("no port")):
            self.assertEqual(self.invoke(["--port", "fake"]), 2)
        self.assertEqual(self.invoke(["--port", "fake", "--fclk", "0"]), 2)

    def test_throughput_loopback_read_only(self):
        from uart_transport import open_port
        with open_port("loop://", 115200, 1) as port:
            port.write(b"junk" + b"".join(report(i, 100*i) for i in range(1, 5)))
            c = capture([])
            c["report_segments"] = []
            perf.receive_reports(port, 4, 1, c)
            self.assertEqual(len(c["raw_reports_hex"]), 4)
            self.assertEqual(port.in_waiting, 0)
            self.assertGreater(c["discarded"]["garbage_bytes"], 0)

    def test_200_reports_no_writes_and_interruption(self):
        port = FakePort(b"".join(report(i, 100*i) for i in range(1, 201)))
        port.close = lambda: None
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"200.json"
            with patch.object(perf, "open_port", return_value=port):
                self.assertEqual(self.invoke(["--port", "fake", "--mode", "throughput", "--reports", "200", "--capture", str(path)]), 0)
            self.assertEqual(port.sent, b"")
            c = json.loads(path.read_text())
            self.assertEqual(c["counters"]["reports_received"], 200)
            self.assertEqual(c["throughput"]["samples_per_cycle"], 512 / 100)
            port.read = lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
            with patch.object(perf, "open_port", return_value=port):
                self.assertEqual(self.invoke(["--port", "fake", "--mode", "throughput", "--capture", str(path)]), 130)
            self.assertIn("Interrupted", json.loads(path.read_text())["error"])

    def test_bad_normal_record_retained_and_replay_fails(self):
        config = FFTConfig(n=8)
        samples = generate_samples(config, random.Random(1))
        port = FakePort(pack_samples(fixed_fft(samples, config)[0], config) + b"x" * 22)
        port.close = lambda: None
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"bad.json"
            with patch.object(perf, "open_port", return_value=port):
                self.assertEqual(self.invoke(["--port", "fake", "--N", "8", "--capture", str(path)]), 1)
            self.assertEqual(json.loads(path.read_text())["raw_reports_hex"], [(b"x" * 22).hex()])
            self.assertEqual(self.invoke(["--replay", str(path)]), 1)

    def test_ambiguous_timestamp_keeps_known_frame_count(self):
        c = capture([report(1, 0), report(10, HALF + 100)])
        self.assertEqual(perf.calculate(c), 1)
        self.assertEqual(c["counters"]["frames_accounted"], 10)
        self.assertEqual(c["counters"]["frames_skipped"], 8)
        self.assertEqual(c["pairs"], [])


if __name__ == "__main__":
    unittest.main()
