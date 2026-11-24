#!/usr/bin/env python3
"""Measure FFT performance using FPGA timestamp reports (normal/throughput)."""

import json
import math
import random
import sys
import time

import fft_uart
from fft_model import FFTConfig, generate_samples, pack_samples, twiddle_table
from perf_reports import HALF, ReportStream, analyze, decode, sequence_ok
from uart_transport import ExchangeError, exchange_frame, list_ports, open_port

SCHEMA = "fft-perf-capture-v1"
CLOCK_WARNING = "FCLK must match the FPGA build: an incorrect value changes us/s, but not cycle counts."


class MeasurementError(RuntimeError):
    pass


def parser():
    p = fft_uart.parser(performance=True)
    p.description = __doc__
    p.add_argument("--mode", choices=("normal", "throughput"), default="normal")
    p.add_argument("--reports", type=int, default=10)
    p.add_argument("--fclk", type=int, default=50_000_000)
    p.add_argument("--no-verify", action="store_true")
    return p


def fft_config(config):
    return FFTConfig(n=config["N"], width=config["width"], integer_width=config["integer_width"],
                     rounding=bool(config["round"]), saturation=bool(config["saturation"]),
                     radix_out_scale=config["radix_out_scale"])


def calculate(capture, no_verify=False):
    mode, config = capture["mode"], capture["config"]
    if mode not in ("normal", "throughput"):
        raise ValueError("invalid capture mode")
    if type(config["fclk"]) is not int or config["fclk"] <= 0:
        raise ValueError("fclk must be a positive integer")
    fft = fft_config(config)
    raws = capture["raw_reports_hex"]
    segments = capture.get("report_segments", [0] * len(raws))
    if len(raws) != len(segments):
        raise ValueError("report segment count does not match records")
    if any(type(s) is not int or s < 0 for s in segments):
        raise ValueError("invalid report segments")
    result = analyze(list(zip(raws, segments)), fft.n, config["fclk"], mode,
                     capture.get("duration_seconds"))
    enabled = mode == "normal" and capture.get(
        "verify_enabled", capture.get("verification", {}).get("enabled", True)
    ) and not no_verify
    verification = {"enabled": enabled, "passed": None, "mismatched_samples": None}
    if enabled:
        frames = capture.get("frames", [])
        if len(frames) != len(raws):
            result["warnings"].append("Missing normal-mode frame data")
        table = capture.get("twiddles_signed") or twiddle_table(fft)
        mismatches = 0
        passed = bool(frames) and len(frames) == len(raws)
        for frame in frames:
            try:
                check = fft_uart.verify_frame(frame, fft, table, capture.get("tolerance_lsb", 0))
                frame["verification"] = check
                mismatches += check["mismatched_samples"]
                passed = passed and check["passed"]
            except (ValueError, KeyError, TypeError) as exc:
                passed = False
                result["warnings"].append("Incomplete frame: " + str(exc))
        verification.update(passed=passed, mismatched_samples=mismatches)
    result["verification"] = verification
    result["counters"]["discarded"] = capture.get("discarded", {})
    if capture.get("discarded"):
        result["warnings"].append("Discarded stream data: " + str(capture["discarded"]))
    target = capture.get("requested_reports")
    if target is not None and result["counters"]["reports_received"] != target:
        result["warnings"].append(f"Missing reports: expected {target}, got {len(result['reports'])}")
    status = 1 if result["warnings"] or capture.get("error") or verification["passed"] is False else 0
    result["warnings"].insert(0, CLOCK_WARNING)
    capture.update(result)
    return status


def display(capture):
    c = capture["config"]
    print(f"Mode: {capture['mode']}  N={c['N']}  FCLK={c['fclk']/1e6:.3f} MHz  baud={c['baudrate']}")
    print("Warning: " + CLOCK_WARNING)
    if capture["mode"] == "normal":
        print("frame    core_gap    export   service   latency*   input_span* (cycles)")
        for r in capture["reports"]:
            v = r["cycles"]
            print(f"{r['seq']:5} {v['core_gap']:11} {v['export']:9} {v['service']:9} {v['latency']:10} {v['input_span']:13}")
        print("* wire-limited: latency and input_span include UART input; period is also host/wire limited.")
    counts = capture["counters"]
    print(f"Reports: {counts['reports_received']}  Frames: {counts['frames_accounted']}  Skipped: {counts['frames_skipped']}  Seq wraps: {counts['seq_wraps']}")
    for units, summaries in (("cycles", capture["summary"]), ("us", capture["summary_us"])):
        print(f"{'metric':14} {'min':>11} {'mean':>11} {'max':>11} {'std':>11} {'p50':>11} {'p95':>11} {'p99':>11} ({units})")
        for metric, stats in summaries.items():
            text = " ".join(f"{stats[k]:11.3f}" for k in ("min", "mean", "max", "std", "p50", "p95", "p99")) if stats else "---"
            print(f"{metric:14} {text}")
    print("Throughput: " + ", ".join(f"{k}={v:.6g}" if v is not None else f"{k}=---" for k, v in capture["throughput"].items()))
    v = capture["verification"]
    if v["enabled"]:
        print(f"Data: {'PASS' if v['passed'] else 'FAIL'}; mismatched samples={v['mismatched_samples']}")
    else:
        print("Data verification: disabled")
    for warning in capture["warnings"][1:]:
        print("Warning: " + warning)
    if capture.get("error"):
        print("Error: " + capture["error"])


def receive_reports(port, count, timeout, capture, clock=time.perf_counter):
    stream = ReportStream()
    start = clock()
    total_deadline = start + count * timeout
    deadline = min(start + timeout, total_deadline)
    try:
        while len(capture["raw_reports_hex"]) < count:
            remaining = min(deadline, total_deadline) - clock()
            if remaining <= 0:
                raise MeasurementError("Report timeout: no valid report; check firmware mode and reset FPGA manually")
            port.timeout = min(.1, remaining)
            chunk = port.read(min(4096, max(1, getattr(port, "in_waiting", 0))))
            for raw, record in stream.feed(chunk):
                capture["raw_reports_hex"].append(raw.hex())
                capture["report_segments"].append(record["segment"])
                deadline = min(clock() + timeout, total_deadline)
                if len(capture["raw_reports_hex"]) == count:
                    # A read may contain future reports; these are outside the
                    # requested observation window, not corrupt trailing data.
                    return
    finally:
        if len(capture["raw_reports_hex"]) < count:
            stream.finish()
        capture["discarded"] = dict(stream.discarded)
        capture["duration_seconds"] = clock() - start


def run(args):
    fft = fft_uart.config_from_args(args)
    if args.frames <= 0 or args.reports <= 0 or args.baudrate <= 0 or args.fclk <= 0 or args.tolerance_lsb < 0:
        raise ValueError("frames/reports/baudrate/fclk must be positive; tolerance must be nonnegative")
    auto = max(2, 3 * (2 * (fft.frame_bytes + 22) * 10 / args.baudrate) + 1) if args.mode == "normal" else max(2, 5 * 22 * 10 / args.baudrate)
    timeout = auto if args.timeout is None else args.timeout
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if args.mode == "throughput":
        timeout = max(timeout, auto)
    table = twiddle_table(fft, args.twiddle_rom) if args.mode == "normal" else None
    rng = random.Random(args.seed)
    first = generate_samples(fft, rng, args.pattern, args.amplitude, args.tone_bin) if args.mode == "normal" else None
    capture = {
        "schema": SCHEMA, "mode": args.mode,
        "config": {"port": args.port, "baudrate": args.baudrate, "fclk": args.fclk, "N": fft.n,
                   "width": fft.width, "integer_width": fft.integer_width, "round": int(fft.rounding),
                   "saturation": int(fft.saturation), "radix_out_scale": fft.radix_out_scale},
        "raw_reports_hex": [], "report_segments": [], "frames": [], "discarded": {},
        "twiddles_signed": table, "verify_enabled": args.mode == "normal" and not args.no_verify,
        "tolerance_lsb": args.tolerance_lsb,
        "requested_reports": args.frames if args.mode == "normal" else args.reports,
        "timeout": timeout, "error": None,
    }
    # Port-open failures are configuration/I/O errors (2), not missing reports (1).
    port = open_port(args.port, args.baudrate, timeout)
    status = 0
    start = time.perf_counter()
    try:
        if args.mode == "throughput":
            receive_reports(port, args.reports, timeout, capture)
        else:
            previous = None
            for i in range(args.frames):
                samples = first if i == 0 else generate_samples(fft, rng, args.pattern, args.amplitude, args.tone_bin)
                payload = pack_samples(samples, fft)
                frame = {"input_signed": samples, "transmitted_hex": payload.hex(), "received_hex": ""}
                capture["frames"].append(frame)
                try:
                    transaction = exchange_frame(port, payload, timeout, response_size=fft.frame_bytes + 22)
                except ExchangeError as exc:
                    frame["partial_response_hex"] = exc.received.hex()
                    raise
                frame["received_hex"] = transaction.received[:fft.frame_bytes].hex()
                raw = transaction.received[fft.frame_bytes:]
                frame["raw_report_hex"] = raw.hex()
                capture["raw_reports_hex"].append(raw.hex())
                capture["report_segments"].append(0)
                record = decode(raw)
                if previous and not sequence_ok(record, previous, "normal"):
                    raise MeasurementError("Nonconsecutive normal frame_seq; reset FPGA manually")
                previous = record
    except KeyboardInterrupt:
        capture["error"] = "Interrupted; reset FPGA manually before retrying"
        status = 130
    except (OSError, RuntimeError, ValueError) as exc:
        capture["error"] = str(exc) + "; reset FPGA manually before retrying"
        status = 1
    finally:
        port.close()
        capture["duration_seconds"] = time.perf_counter() - start
    status = status or calculate(capture)
    if "summary" not in capture:
        calculate(capture)
    display(capture)
    if args.capture:
        save(args.capture, capture)
    return status


def save(path, capture):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(capture, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.dry_run or args.discard_input:
            raise ValueError("perf does not support --dry-run/--discard-input; use --replay or reset FPGA manually")
        if args.list_ports:
            for item in list_ports():
                print(f"{item['port']}: {item['description']}")
            return 0
        if args.replay:
            capture = json.loads(args.replay.read_text(encoding="utf-8"))
            if not isinstance(capture, dict) or capture.get("schema") != SCHEMA:
                raise ValueError("unsupported capture schema")
            code = calculate(capture, args.no_verify)
            display(capture)
            if args.capture:
                if args.capture.resolve() == args.replay.resolve():
                    raise ValueError("capture output must differ from replay input")
                save(args.capture, capture)
            return code
        if not args.port:
            p.error("choose --port or --replay")
        return run(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
