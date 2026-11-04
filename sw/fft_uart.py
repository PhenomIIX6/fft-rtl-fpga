#!/usr/bin/env python3
"""Generate, transmit, receive and verify FFT frames; see sw/README.md."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import random
import sys

from fft_model import (
    FFTConfig, compare, fixed_fft, floating_fft, generate_samples,
    pack_samples, twiddle_table, unpack_samples, validate_samples,
)
from uart_transport import ExchangeError, exchange_frame, list_ports, open_port

SCHEMA = "fft-uart-capture-v1"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--port", help="UART device, e.g. COM3 or /dev/ttyUSB0")
    mode.add_argument("--dry-run", action="store_true", help="software exercise only; no hardware validation")
    mode.add_argument("--replay", type=Path, help="verify received bytes in an existing JSON capture")
    mode.add_argument("--list-ports", action="store_true", help="list available UART ports and exit")
    p.add_argument("--n", "--N", type=int, help="FFT length (default 512)")
    p.add_argument("--width", "--WIDTH", type=int, help="bits per component (default 16)")
    p.add_argument("--integer-width", "--INTEGER_WIDTH", type=int, help="integer bits including sign (default 2)")
    rounding = p.add_mutually_exclusive_group()
    rounding.add_argument("--round", "--ROUND", type=int, choices=(0, 1), dest="rounding", help="1: ties to even, 0: truncate (default 1)")
    rounding.add_argument("--truncation", "--TRUNCATION", dest="rounding", action="store_const", const=0, help="alias for --round 0")
    p.add_argument("--saturation", "--SATURATION", type=int, choices=(0, 1), help="1: saturate, 0: wrap (default 1)")
    p.add_argument("--radix-out-scale", "--RADIX_OUT_SCALE", type=int, help="right shift per stage (default 1)")
    p.add_argument("--twiddle-rom", type=Path, help="exact binary-text quarter-wave ROM used by the FPGA")
    p.add_argument("--baudrate", type=int, default=115200)
    p.add_argument("--timeout", type=float, help="whole-frame TX+RX deadline; default max(2s, 3*wire time+1s)")
    p.add_argument("--discard-input", action="store_true", help="discard stale host RX bytes once on open; does not reset FPGA")
    p.add_argument("--frames", type=int, default=1)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--pattern", choices=("random", "zero", "impulse", "dc", "tone"), default="random")
    p.add_argument("--amplitude", type=float, default=0.5, help="maximum absolute value per input component")
    p.add_argument("--tone-bin", type=int, default=1)
    p.add_argument("--tolerance-lsb", type=int, default=0, help="allowed component difference from integer reference")
    p.add_argument("--capture", type=Path, help="save inputs, raw UART bytes, references, metrics and errors as JSON")
    return p


def config_from_args(args) -> FFTConfig:
    defaults = asdict(FFTConfig())
    for name in defaults:
        value = getattr(args, name)
        if value is not None:
            defaults[name] = bool(value) if name in ("rounding", "saturation") else value
    return FFTConfig(**defaults)


def verify_frame(frame: dict, config: FFTConfig, twiddles: list, tolerance: int) -> dict:
    samples = frame["input_signed"]
    validate_samples(samples, config)
    tx = bytes.fromhex(frame["transmitted_hex"])
    if tx != pack_samples(samples, config):
        raise ValueError("capture's transmitted bytes disagree with input_signed")
    received = unpack_samples(bytes.fromhex(frame["received_hex"]), config)
    expected, statistics = fixed_fft(samples, config, twiddles)
    metrics = compare(received, expected, floating_fft(samples, config), config, tolerance)
    return {**metrics, **statistics}


def print_frame(index: int, result: dict, exchange: dict | None) -> None:
    status = "PASS" if result["passed"] else "FAIL"
    print(
        f"Frame {index}: {status}; mismatches={result['mismatched_samples']}; "
        f"max error={result['max_component_error_lsb']} LSB; "
        f"ideal RMSE={result['ideal_rmse']:.6g}; "
        f"overflows={result['overflow_components']}"
    )
    if result["ideal_sqnr_db"] is not None:
        print(f"  Ideal SQNR: {result['ideal_sqnr_db']:.2f} dB")
    for mismatch in result["first_mismatches"]:
        print(f"  [{mismatch['index']}] got {mismatch['actual']}, expected {mismatch['expected']}")
    if exchange is not None:
        print(
            f"  UART round trip: {exchange['roundtrip_seconds']:.6f} s; "
            f"{exchange['roundtrip_samples_per_second']:.1f} samples/s "
            "(includes UART and host latency)"
        )


def replay(args) -> int:
    capture = json.loads(args.replay.read_text(encoding="utf-8"))
    if not isinstance(capture, dict) or capture.get("schema") != SCHEMA:
        raise ValueError("unsupported capture schema")
    if not isinstance(capture.get("config"), dict):
        raise ValueError("capture must contain a config object")
    config = FFTConfig(**capture["config"])
    for name, recorded in capture["config"].items():
        override = getattr(args, name)
        if override is not None and override != recorded:
            raise ValueError(f"--replay uses captured {name}; conflicting override")
    if args.twiddle_rom is not None:
        raise ValueError("--replay uses captured twiddles; omit --twiddle-rom")
    if args.capture is not None:
        raise ValueError("--replay is read-only; omit --capture")
    twiddles = capture["twiddles_signed"]
    validate_samples(twiddles, config)
    print(f"REPLAY: original source={capture['source']}; no UART traffic")
    if not isinstance(capture["frames"], list) or not capture["frames"]:
        raise ValueError("capture contains no frames")
    passed = True
    for index, frame in enumerate(capture["frames"], 1):
        if not isinstance(frame, dict):
            raise ValueError(f"frame {index} must be an object")
        if frame.get("error"):
            print(f"Frame {index}: incomplete transaction: {frame['error']}")
            passed = False
            continue
        result = verify_frame(frame, config, twiddles, args.tolerance_lsb)
        print_frame(index, result, frame.get("exchange"))
        passed = passed and result["passed"]
    return 0 if passed and not capture.get("error") else 1


def run(args) -> int:
    config = config_from_args(args)
    if args.frames <= 0 or args.baudrate <= 0 or args.tolerance_lsb < 0:
        raise ValueError("frames and baudrate must be positive; tolerance-lsb must be nonnegative")
    wire_seconds = 2 * config.frame_bytes * 10 / args.baudrate
    timeout = args.timeout if args.timeout is not None else max(2.0, 3 * wire_seconds + 1)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    twiddles = twiddle_table(config, args.twiddle_rom)
    rng = random.Random(args.seed)
    # Validate generation options before opening a real serial port.
    first_samples = generate_samples(config, rng, args.pattern, args.amplitude, args.tone_bin)
    source = "dry-run" if args.dry_run else "hardware"
    capture = {
        "schema": SCHEMA, "source": source, "config": asdict(config),
        "twiddles_signed": twiddles,
        "generator": {"seed": args.seed, "pattern": args.pattern, "amplitude": args.amplitude, "tone_bin": args.tone_bin},
        "transport": {"port": args.port, "baudrate": args.baudrate, "timeout": timeout},
        "frames": [],
    }
    print(f"{'DRY-RUN (software only)' if args.dry_run else 'HARDWARE'}: N={config.n}, WIDTH={config.width}, INTEGER_WIDTH={config.integer_width}")
    print(f"Frame: {config.frame_bytes} bytes per direction; positive-exponent transform; parameters must match FPGA build")
    if not args.dry_run:
        print(f"UART 8N1: {args.baudrate} baud; nominal TX+RX wire time >= {wire_seconds:.4f} s; timeout={timeout:.3f} s")
    port = None
    passed = True
    try:
        if not args.dry_run:
            port = open_port(args.port, args.baudrate, timeout)
            if args.discard_input:
                port.reset_input_buffer()
        for index in range(args.frames):
            samples = first_samples if index == 0 else generate_samples(config, rng, args.pattern, args.amplitude, args.tone_bin)
            payload = pack_samples(samples, config)
            frame = {"input_signed": samples, "transmitted_hex": payload.hex(), "received_hex": ""}
            capture["frames"].append(frame)
            expected, statistics = fixed_fft(samples, config, twiddles)
            frame["expected_signed"] = expected
            if args.dry_run:
                received_payload = pack_samples(expected, config)
                exchange_metrics = None
            else:
                transaction = exchange_frame(port, payload, timeout)
                received_payload = transaction.received
                exchange_metrics = transaction.metrics(config.n, len(payload))
            frame["received_hex"] = received_payload.hex()
            frame["exchange"] = exchange_metrics
            received = unpack_samples(received_payload, config)
            frame["received_signed"] = received
            verification = compare(received, expected, floating_fft(samples, config), config, args.tolerance_lsb)
            frame["verification"] = {**verification, **statistics}
            print_frame(index + 1, frame["verification"], exchange_metrics)
            passed = passed and verification["passed"]
    except (Exception, KeyboardInterrupt) as exc:
        capture["error"] = str(exc) or type(exc).__name__
        if capture["frames"]:
            frame = capture["frames"][-1]
            frame["error"] = capture["error"]
            if isinstance(exc, ExchangeError):
                frame["received_hex"] = exc.received.hex()
                frame["sent_bytes"] = exc.sent
        raise
    finally:
        if port is not None:
            port.close()
        if args.capture is not None:
            args.capture.parent.mkdir(parents=True, exist_ok=True)
            args.capture.write_text(json.dumps(capture, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            print(f"Capture saved: {args.capture.resolve()}")
    if args.dry_run:
        print("Software exercise completed; FPGA correctness and core throughput were not measured.")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    if not (args.port or args.dry_run or args.replay or args.list_ports):
        p.error("choose --port, --dry-run, --replay or --list-ports")
    try:
        if args.tolerance_lsb < 0:
            raise ValueError("tolerance-lsb must be nonnegative")
        if args.list_ports:
            ports = list_ports()
            for item in ports:
                print(f"{item['port']}: {item['description']}")
            if not ports:
                print("No UART ports found")
            return 0
        return replay(args) if args.replay else run(args)
    except KeyboardInterrupt:
        print("Interrupted; reset the FPGA before another transaction.", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
