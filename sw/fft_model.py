"""Host data format and reference models for the positive-exponent FFT RTL.

Integer samples are (real, imaginary) pairs in two's complement Q format.
The fixed reference implements the intended arithmetic, so RTL arithmetic bugs
remain visible rather than being copied into the expected result.
"""

from __future__ import annotations

import cmath
from dataclasses import dataclass
import math
from pathlib import Path
import random
from typing import Sequence

Sample = tuple[int, int]


@dataclass(frozen=True)
class FFTConfig:
    n: int = 512
    width: int = 16
    integer_width: int = 2
    rounding: bool = True
    saturation: bool = True
    radix_out_scale: int = 1

    def __post_init__(self) -> None:
        if any(type(value) is not int for value in (self.n, self.width, self.integer_width, self.radix_out_scale)):
            raise ValueError("N, WIDTH, INTEGER_WIDTH and RADIX_OUT_SCALE must be integers")
        if type(self.rounding) is not bool or type(self.saturation) is not bool:
            raise ValueError("rounding and saturation must be booleans")
        if self.n < 8 or self.n & (self.n - 1):
            raise ValueError("N must be a power of two >= 8 (twiddle ROM constraint)")
        if not 3 <= self.width <= 32:
            raise ValueError("WIDTH must be between 3 and 32 for this host model")
        if not 2 <= self.integer_width < self.width:
            raise ValueError("require 2 <= INTEGER_WIDTH < WIDTH to represent twiddles")
        if not 0 <= self.radix_out_scale <= self.width:
            raise ValueError("RADIX_OUT_SCALE must be between 0 and WIDTH")

    @property
    def frac_width(self) -> int:
        return self.width - self.integer_width

    @property
    def unit(self) -> int:
        return 1 << self.frac_width

    @property
    def minimum(self) -> int:
        return -(1 << (self.width - 1))

    @property
    def maximum(self) -> int:
        return (1 << (self.width - 1)) - 1

    @property
    def stages(self) -> int:
        return self.n.bit_length() - 1

    @property
    def bytes_per_sample(self) -> int:
        return (2 * self.width + 7) // 8

    @property
    def frame_bytes(self) -> int:
        return self.n * self.bytes_per_sample


def signed(value: int, width: int) -> int:
    value &= (1 << width) - 1
    return value - (1 << width) if value & (1 << (width - 1)) else value


def validate_samples(samples: Sequence[Sequence[int]], config: FFTConfig) -> None:
    if len(samples) != config.n:
        raise ValueError(f"expected {config.n} samples, got {len(samples)}")
    for index, point in enumerate(samples):
        if len(point) != 2 or any(type(x) is not int for x in point):
            raise ValueError(f"sample {index} must be a pair of signed integers")
        if any(not config.minimum <= x <= config.maximum for x in point):
            raise ValueError(f"sample {index} is outside the signed WIDTH-bit range")


def pack_samples(samples: Sequence[Sequence[int]], config: FFTConfig) -> bytes:
    """Re in high WIDTH bits, Im in low WIDTH bits; whole word little endian.

Non-byte-aligned components are packed together without component padding.
Only the unused high bits of the final byte of each sample are padded.
"""
    validate_samples(samples, config)
    mask = (1 << config.width) - 1
    return b"".join(
        (((re & mask) << config.width) | (im & mask)).to_bytes(
            config.bytes_per_sample, "little"
        )
        for re, im in samples
    )


def unpack_samples(payload: bytes, config: FFTConfig) -> list[Sample]:
    if len(payload) != config.frame_bytes:
        raise ValueError(f"expected {config.frame_bytes} bytes, got {len(payload)}")
    points = []
    for offset in range(0, len(payload), config.bytes_per_sample):
        word = int.from_bytes(payload[offset:offset + config.bytes_per_sample], "little")
        if word >> (2 * config.width):
            raise ValueError(f"nonzero padding bits in sample {len(points)}")
        points.append((signed(word >> config.width, config.width), signed(word, config.width)))
    return points


def quantize(value: float, config: FFTConfig) -> int:
    if not math.isfinite(value):
        raise ValueError("sample values must be finite")
    result = round(value * config.unit)
    if not config.minimum <= result <= config.maximum:
        raise ValueError("input amplitude exceeds the fixed-point range")
    return result


def generate_samples(
    config: FFTConfig, rng: random.Random, pattern: str = "random",
    amplitude: float = 0.5, tone_bin: int = 1,
) -> list[Sample]:
    """Amplitude is an absolute bound for each component, not a full-scale ratio."""
    if not math.isfinite(amplitude) or not 0 <= amplitude <= config.maximum / config.unit:
        raise ValueError("amplitude must be finite, nonnegative and representable")
    level = quantize(amplitude, config)
    if pattern == "random":
        return [(rng.randint(-level, level), rng.randint(-level, level)) for _ in range(config.n)]
    if pattern == "zero":
        return [(0, 0)] * config.n
    if pattern == "impulse":
        return [(level, 0)] + [(0, 0)] * (config.n - 1)
    if pattern == "dc":
        return [(level, 0)] * config.n
    if pattern == "tone":
        if not 0 <= tone_bin < config.n:
            raise ValueError("tone-bin must be in [0, N)")
        # Negative input exponent gives a peak at positive tone_bin in this RTL.
        return [
            (quantize(amplitude * math.cos(2 * math.pi * tone_bin * k / config.n), config),
             quantize(-amplitude * math.sin(2 * math.pi * tone_bin * k / config.n), config))
            for k in range(config.n)
        ]
    raise ValueError(f"unknown pattern: {pattern}")


def twiddle_table(config: FFTConfig, rom_path: Path | None = None) -> list[Sample]:
    """Reconstruct all quadrants from the RTL's quarter-wave ROM.

An optional ROM is the exact binary-text file used to build the FPGA. Otherwise
use the C generator's positive-quadrant round-half-up rule, then integer swaps
and negations just like twiddle_factors_rom.sv.
"""
    quarter = config.n // 4
    if rom_path is None:
        base = [
            (math.floor(math.cos(2 * math.pi * k / config.n) * config.unit + 0.5),
             math.floor(math.sin(2 * math.pi * k / config.n) * config.unit + 0.5))
            for k in range(quarter + 1)
        ]
    else:
        lines = rom_path.read_text(encoding="utf-8-sig").splitlines()
        lines = [line.strip() for line in lines if line.strip()]
        if len(lines) != quarter + 1:
            raise ValueError(f"twiddle ROM must contain {quarter + 1} binary words")
        base = []
        for index, line in enumerate(lines):
            if len(line) != 2 * config.width or set(line) - {"0", "1"}:
                raise ValueError(f"invalid WIDTH or binary data in twiddle ROM line {index + 1}")
            word = int(line, 2)
            base.append((signed(word >> config.width, config.width), signed(word, config.width)))
    table = []
    for k in range(config.n):
        quadrant, offset = divmod(k, quarter)
        if offset == 0 and quadrant % 2:
            re, im = base[quarter]
            quadrant -= 1
        else:
            re, im = base[offset]
        re, im = ((re, im), (-im, re), (-re, -im), (im, -re))[quadrant]
        table.append((signed(re, config.width), signed(im, config.width)))
    return table


def reduce_component(value: int, config: FFTConfig) -> tuple[int, bool]:
    """Arithmetic shift (floor for negatives), optionally round ties to even."""
    shift = config.frac_width + config.radix_out_scale
    quotient, remainder = divmod(value, 1 << shift)
    if config.rounding:
        half = 1 << (shift - 1)
        if remainder > half or (remainder == half and quotient & 1):
            quotient += 1
    overflow = not config.minimum <= quotient <= config.maximum
    if config.saturation:
        result = min(config.maximum, max(config.minimum, quotient))
    else:
        result = signed(quotient, config.width)
    return result, overflow


def _bit_reversed(values: Sequence, stages: int) -> list:
    return [values[int(f"{index:0{stages}b}"[::-1], 2)] for index in range(len(values))]


def fixed_fft(
    samples: Sequence[Sequence[int]], config: FFTConfig,
    twiddles: Sequence[Sample] | None = None,
) -> tuple[list[Sample], dict]:
    """Integer reference with quantized ROM and per-butterfly narrowing.

The 2*WIDTH-bit multiplier outputs wrap before the extended addition, matching
the datapath widths. The subsequent saturation uses mathematical signed range
checks; it intentionally does not reproduce bugs in round_trunc_scale.sv.
"""
    validate_samples(samples, config)
    twiddles = twiddle_table(config) if twiddles is None else twiddles
    if len(twiddles) != config.n:
        raise ValueError("twiddle table size does not match N")
    result = _bit_reversed([tuple(x) for x in samples], config.stages)
    overflows = 0
    span = 2
    while span <= config.n:
        half = span // 2
        for start in range(0, config.n, span):
            for offset in range(half):
                left = result[start + offset]
                right = result[start + offset + half]
                index = offset * (config.n // span)
                for address, tw_index in (
                    (start + offset, index), (start + offset + half, index + config.n // 2)
                ):
                    wr, wi = twiddles[tw_index]
                    pr = signed(wr * right[0] - wi * right[1], 2 * config.width)
                    pi = signed(wr * right[1] + wi * right[0], 2 * config.width)
                    re, re_over = reduce_component((left[0] << config.frac_width) + pr, config)
                    im, im_over = reduce_component((left[1] << config.frac_width) + pi, config)
                    result[address] = (re, im)
                    overflows += re_over + im_over
        span *= 2
    return result, {"overflow_components": overflows}


def floating_fft(samples: Sequence[Sequence[int]], config: FFTConfig) -> list[complex]:
    """Ideal positive-sign FFT with stage scaling, equivalent to ifft*N/scale."""
    validate_samples(samples, config)
    values = [complex(re / config.unit, im / config.unit) for re, im in samples]
    result = _bit_reversed(values, config.stages)
    span = 2
    stage_divisor = 1 << config.radix_out_scale
    while span <= config.n:
        half = span // 2
        for start in range(0, config.n, span):
            for offset in range(half):
                left, right = result[start + offset], result[start + offset + half]
                product = cmath.exp(2j * math.pi * offset / span) * right
                result[start + offset] = (left + product) / stage_divisor
                result[start + offset + half] = (left - product) / stage_divisor
        span *= 2
    return result


def compare(
    actual: Sequence[Sequence[int]], expected: Sequence[Sequence[int]],
    ideal: Sequence[complex], config: FFTConfig, tolerance_lsb: int = 0,
) -> dict:
    validate_samples(actual, config)
    validate_samples(expected, config)
    if len(ideal) != config.n or tolerance_lsb < 0:
        raise ValueError("invalid ideal reference length or tolerance")
    errors = [max(abs(a - e) for a, e in zip(got, want)) for got, want in zip(actual, expected)]
    values = [complex(re / config.unit, im / config.unit) for re, im in actual]
    squared = [abs(got - want) ** 2 for got, want in zip(values, ideal)]
    error_power = sum(squared)
    signal_power = sum(abs(value) ** 2 for value in ideal)
    sqnr = 10 * math.log10(signal_power / error_power) if signal_power and error_power else None
    mismatches = [i for i, error in enumerate(errors) if error > tolerance_lsb]
    return {
        "passed": not mismatches,
        "tolerance_lsb": tolerance_lsb,
        "mismatched_samples": len(mismatches),
        "max_component_error_lsb": max(errors),
        "first_mismatches": [
            {"index": i, "actual": list(actual[i]), "expected": list(expected[i])}
            for i in mismatches[:8]
        ],
        "ideal_max_complex_error": math.sqrt(max(squared)),
        "ideal_rmse": math.sqrt(error_power / config.n),
        "ideal_sqnr_db": sqnr,
        "ideal_error_is_zero": error_power == 0,
    }
