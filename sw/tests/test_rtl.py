"""Optional integration tests against working-tree RTL using Verilator 5+.

Set VERILATOR to an executable path, or put verilator on PATH. A C++ compiler
and make are required. The complete UART -> FFT -> UART path is used.
"""

import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fft_model import FFTConfig, fixed_fft, generate_samples, pack_samples, twiddle_table

ROOT = Path(__file__).resolve().parents[2]
VERILATOR = os.environ.get("VERILATOR") or shutil.which("verilator")


@unittest.skipUnless(VERILATOR, "Verilator not found; set VERILATOR for RTL integration tests")
class RTLTests(unittest.TestCase):
    def check_frames(self, config, pattern="random", amplitude=0.5):
        rng = random.Random(91)
        frames = [generate_samples(config, rng, pattern, amplitude) for _ in range(2)]
        table = twiddle_table(config)
        expected = b"".join(pack_samples(fixed_fft(frame, config, table)[0], config) for frame in frames)
        with tempfile.TemporaryDirectory(prefix="fft-rtl-") as folder:
            directory = Path(folder)
            payload = b"".join(pack_samples(frame, config) for frame in frames)
            (directory / "input.hex").write_text("\n".join(f"{byte:02x}" for byte in payload), encoding="ascii")
            mask = (1 << config.width) - 1
            (directory / "twiddle_factors.bin").write_text("\n".join(
                f"{re & mask:0{config.width}b}{im & mask:0{config.width}b}"
                for re, im in table[:config.n // 4 + 1]
            ), encoding="ascii")
            sources = [ROOT / "rtl" / name for name in ("top.sv", "byte_packer.sv", "byte_unpacker.sv")]
            sources += sorted((ROOT / "rtl" / "uart").glob("*.sv"))
            sources += sorted((ROOT / "fft-rtl" / "rtl").rglob("*.sv"))
            sources += [Path(__file__).with_name("uart_fft_tb.sv")]
            parameters = {
                "N": config.n, "WIDTH": config.width, "INTEGER_WIDTH": config.integer_width,
                "ROUND": int(config.rounding), "SATURATION": int(config.saturation),
                "RADIX_OUT_SCALE": config.radix_out_scale,
            }
            command = [str(VERILATOR), "--binary", "--timing", "-Wno-fatal", "--top-module", "uart_fft_tb", "--Mdir", "obj_dir", "-j", "2", "-o", "simulation.exe"]
            command += [f"-G{key}={value}" for key, value in parameters.items()]
            command += [str(source) for source in sources]
            compiled = subprocess.run(command, cwd=directory, capture_output=True, text=True, timeout=120)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            simulated = subprocess.run([str(directory / "obj_dir" / "simulation.exe")], cwd=directory, capture_output=True, text=True, timeout=45)
            self.assertEqual(simulated.returncode, 0, simulated.stdout + simulated.stderr)
            self.assertIn("UART_FRAMES_DONE", simulated.stdout)
            actual = bytes.fromhex((directory / "output.hex").read_text(encoding="ascii"))
            self.assertEqual(actual, expected, f"UART bytes differ for {config}, pattern={pattern}")

    def test_default_arithmetic_two_frames(self):
        self.check_frames(FFTConfig(n=16))

    def test_non_byte_width_truncation(self):
        self.check_frames(FFTConfig(n=16, width=13, rounding=False))

    def test_unscaled_wrap(self):
        self.check_frames(FFTConfig(n=16, width=8, saturation=False, radix_out_scale=0), amplitude=1.5)

    def test_unscaled_saturation(self):
        self.check_frames(FFTConfig(n=16, width=8, radix_out_scale=0), amplitude=1.5)

    def test_tone_sign_and_order(self):
        self.check_frames(FFTConfig(n=16), "tone")

    def test_default_length_512(self):
        self.check_frames(FFTConfig())

    def test_minimum_length_and_64_bit_complex_word(self):
        self.check_frames(FFTConfig(n=8, width=32))

    def test_alternate_integer_width_and_stage_scale(self):
        self.check_frames(FFTConfig(n=32, integer_width=3, radix_out_scale=2))


if __name__ == "__main__":
    unittest.main()
