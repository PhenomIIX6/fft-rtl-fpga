import cmath
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import math
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fft_model import (
    FFTConfig, compare, fixed_fft, floating_fft, generate_samples, pack_samples,
    reduce_component, signed, twiddle_table, unpack_samples,
)
import fft_uart
from uart_transport import ExchangeError, exchange_frame


class FormatTests(unittest.TestCase):
    def test_wire_layout_matches_known_word(self):
        config = FFTConfig(n=8)
        samples = [(16384, 8192), (-32768, -1)] + [(0, 0)] * 6
        wire = pack_samples(samples, config)
        self.assertEqual(wire[:8], bytes.fromhex("00 20 00 40 ff ff 00 80"))
        self.assertEqual(wire[:4], struct.pack("<hh", 8192, 16384))
        self.assertEqual(unpack_samples(wire, config), samples)

    def test_non_byte_width_packs_whole_complex_word(self):
        config = FFTConfig(n=8, width=5)
        samples = [(1, -1), (-16, 15)] + [(0, 0)] * 6
        wire = pack_samples(samples, config)
        self.assertEqual(wire[:4], bytes.fromhex("3f 00 0f 02"))
        self.assertEqual(len(wire), 16)
        self.assertEqual(unpack_samples(wire, config), samples)
        with self.assertRaisesRegex(ValueError, "padding"):
            unpack_samples(wire[:1] + b"\x80" + wire[2:], config)

    def test_random_all_supported_widths(self):
        rng = random.Random(9)
        for width in range(3, 33):
            config = FFTConfig(n=8, width=width)
            samples = [(config.minimum, config.maximum)] + [
                (rng.randint(config.minimum, config.maximum), rng.randint(config.minimum, config.maximum))
                for _ in range(7)
            ]
            self.assertEqual(unpack_samples(pack_samples(samples, config), config), samples)

    def test_invalid_configs_and_lengths(self):
        for kwargs in ({"n": 1}, {"n": 12}, {"integer_width": 1}, {"width": 33}, {"radix_out_scale": -1}):
            with self.assertRaises(ValueError):
                FFTConfig(**kwargs)
        with self.assertRaises(ValueError):
            pack_samples([(32768, 0)] * 8, FFTConfig(n=8))
        with self.assertRaises(ValueError):
            unpack_samples(b"", FFTConfig(n=8))


class ReferenceTests(unittest.TestCase):
    def test_ties_to_even_and_negative_arithmetic_truncation(self):
        config = FFTConfig(n=8, width=4, integer_width=2, radix_out_scale=0)
        for numerator, expected in ((2, 0), (6, 2), (-2, 0), (-6, -2), (-3, -1), (3, 1)):
            self.assertEqual(reduce_component(numerator, config), (expected, False))
        self.assertEqual(reduce_component(-1, replace(config, rounding=False)), (-1, False))

    def test_saturation_and_wrap(self):
        config = FFTConfig(n=8, width=4, integer_width=2, radix_out_scale=0)
        self.assertEqual(reduce_component(40, config), (7, True))
        self.assertEqual(reduce_component(-40, config), (-8, True))
        self.assertEqual(reduce_component(40, replace(config, saturation=False)), (-6, True))

    def test_twiddle_cardinal_points_and_symmetry(self):
        config = FFTConfig(n=32)
        table = twiddle_table(config)
        self.assertEqual([table[i] for i in (0, 8, 16, 24)], [(16384, 0), (0, 16384), (-16384, 0), (0, -16384)])
        for i in range(16):
            self.assertEqual(table[i + 16], tuple(-x for x in table[i]))
        with tempfile.TemporaryDirectory() as folder:
            rom = Path(folder) / "twiddle_factors.bin"
            mask = (1 << config.width) - 1
            rom.write_text("\n".join(f"{re & mask:016b}{im & mask:016b}" for re, im in table[:9]), encoding="utf-8")
            self.assertEqual(twiddle_table(config, rom), table)
            rom.write_text("0\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                twiddle_table(config, rom)

    def test_floating_reference_against_direct_positive_dft(self):
        samples = generate_samples(FFTConfig(n=16), random.Random(8))
        for scale in (0, 1, 2):
            config = FFTConfig(n=16, radix_out_scale=scale)
            result = floating_fft(samples, config)
            for k in range(config.n):
                expected = sum(
                    complex(re, im) / config.unit * cmath.exp(2j * math.pi * k * j / config.n)
                    for j, (re, im) in enumerate(samples)
                ) / (1 << (config.stages * scale))
                self.assertLess(abs(result[k] - expected), 1e-12)

    def test_dc_impulse_tone_and_zero(self):
        config = FFTConfig(n=16)
        for pattern in ("zero", "impulse", "dc", "tone"):
            samples = generate_samples(config, random.Random(1), pattern, 0.5, 3)
            result, _ = fixed_fft(samples, config)
            ideal = floating_fft(samples, config)
            self.assertLess(max(abs(complex(*point) / config.unit - want) for point, want in zip(result, ideal)), 3 / config.unit)
            if pattern == "tone":
                self.assertEqual(max(range(16), key=lambda i: abs(complex(*result[i]))), 3)
        dc, statistics = fixed_fft([(config.unit, 0)] * 16, config)
        self.assertEqual(dc, [(config.unit, 0)] + [(0, 0)] * 15)
        self.assertEqual(statistics["overflow_components"], 0)

    def test_intermediate_overflows_are_not_final_clipping(self):
        config = FFTConfig(n=8, width=8, integer_width=2, radix_out_scale=0)
        saturated, statistics = fixed_fft([(100, 0)] * 8, config)
        wrapped, _ = fixed_fft([(100, 0)] * 8, replace(config, saturation=False))
        self.assertEqual(saturated[0], (127, 0))
        self.assertEqual(wrapped[0], (signed(800, 8), 0))
        self.assertGreater(statistics["overflow_components"], 0)

    def test_seed_range_and_bad_amplitudes(self):
        config = FFTConfig(n=8)
        self.assertEqual(generate_samples(config, random.Random(2)), generate_samples(config, random.Random(2)))
        for amplitude in (float("nan"), float("inf"), -1, 2):
            with self.assertRaises(ValueError):
                generate_samples(config, random.Random(1), amplitude=amplitude)

    def test_tolerance_and_zero_signal_metrics(self):
        config = FFTConfig(n=8)
        expected = [(0, 0)] * 8
        actual = [(1, -1)] + expected[1:]
        self.assertFalse(compare(actual, expected, [0j] * 8, config)["passed"])
        self.assertTrue(compare(actual, expected, [0j] * 8, config, 1)["passed"])
        result = compare(expected, expected, [0j] * 8, config)
        self.assertIsNone(result["ideal_sqnr_db"])
        json.dumps(result, allow_nan=False)


class FakePort:
    def __init__(self, response, chunk=3, extra=False):
        self.response = bytearray(response)
        self.chunk = chunk
        self.sent = bytearray()
        self.timeout = 9
        self.write_timeout = 8
        self.extra = extra

    @property
    def in_waiting(self):
        return int(self.extra)

    def write(self, data):
        count = min(2, len(data))
        self.sent.extend(data[:count])
        return count

    def read(self, size):
        count = min(size, self.chunk)
        result = bytes(self.response[:count])
        del self.response[:count]
        return result


class TransportTests(unittest.TestCase):
    def test_partial_writes_and_reads(self):
        payload = bytes(range(32))
        port = FakePort(payload[::-1])
        transaction = exchange_frame(port, payload, 1)
        self.assertEqual(bytes(port.sent), payload)
        self.assertEqual(transaction.received, payload[::-1])
        self.assertEqual((port.timeout, port.write_timeout), (9, 8))
        self.assertGreater(transaction.metrics(8, 32)["roundtrip_samples_per_second"], 0)

    def test_partial_timeout_keeps_bytes(self):
        port = FakePort(b"ab")
        times = iter(i / 100 for i in range(100))
        with self.assertRaisesRegex(ExchangeError, "received 2/8") as error:
            exchange_frame(port, b"12345678", 0.2, clock=lambda: next(times))
        self.assertEqual(error.exception.received, b"ab")
        self.assertEqual(error.exception.sent, 8)
        self.assertEqual((port.timeout, port.write_timeout), (9, 8))

    def test_stale_input_not_silently_discarded(self):
        port = FakePort(b"", extra=True)
        with self.assertRaisesRegex(ExchangeError, "buffered"):
            exchange_frame(port, b"hello", 1)
        self.assertEqual(port.sent, b"")

    def test_serial_exception_preserves_partial_receive(self):
        port = FakePort(b"ab")
        original_read = port.read
        def failing_read(size):
            if not port.response:
                raise OSError("disconnected")
            return original_read(size)
        port.read = failing_read
        with self.assertRaisesRegex(ExchangeError, "disconnected") as error:
            exchange_frame(port, b"12345678", 1)
        self.assertEqual(error.exception.received, b"ab")

    def test_pyserial_loopback(self):
        try:
            from uart_transport import open_port
            port = open_port("loop://", 115200, 1)
        except RuntimeError:
            self.skipTest("pySerial not installed")
        with port:
            payload = bytes(range(64))
            self.assertEqual(exchange_frame(port, payload, 1).received, payload)


class CLITests(unittest.TestCase):
    def invoke(self, args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return fft_uart.main(args)

    def test_capture_replay_and_corrupted_result(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.json"
            self.assertEqual(self.invoke(["--dry-run", "--N", "16", "--frames", "2", "--capture", str(path)]), 0)
            capture = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(capture["source"], "dry-run")
            self.assertIsNone(capture["frames"][0]["exchange"])
            self.assertEqual(self.invoke(["--replay", str(path)]), 0)
            frame = capture["frames"][0]
            wire = bytearray.fromhex(frame["received_hex"])
            wire[0] ^= 1
            frame["received_hex"] = wire.hex()
            path.write_text(json.dumps(capture), encoding="utf-8")
            self.assertEqual(self.invoke(["--replay", str(path)]), 1)
            self.assertEqual(self.invoke(["--replay", str(path), "--tolerance-lsb", "1"]), 0)
            self.assertEqual(self.invoke(["--replay", str(path), "--N", "32"]), 2)

    def test_transport_error_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.json"
            port = FakePort(b"abc")
            port.close = lambda: None
            with patch.object(fft_uart, "open_port", return_value=port), patch.object(
                fft_uart, "exchange_frame", side_effect=ExchangeError("test timeout", b"abc", 32)
            ):
                self.assertEqual(self.invoke(["--port", "test", "--N", "8", "--capture", str(path)]), 2)
            capture = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(capture["frames"][0]["received_hex"], "616263")
            self.assertEqual(self.invoke(["--replay", str(path)]), 1)

    def test_input_checked_before_port_open(self):
        with patch.object(fft_uart, "open_port") as open_mock:
            self.assertEqual(self.invoke(["--port", "COM3", "--amplitude", "nan"]), 2)
            self.assertEqual(self.invoke(["--port", "COM3", "--timeout", "inf"]), 2)
            self.assertEqual(self.invoke(["--port", "COM3", "--frames", "0"]), 2)
            open_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
