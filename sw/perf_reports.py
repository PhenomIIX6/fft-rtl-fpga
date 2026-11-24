"""22-byte FPGA performance records, streaming synchronization and statistics."""

from collections import Counter
import math
import statistics
import struct

SYNC = b"\xa5\x5a"
RECORD = struct.Struct("<2s5I")
MOD = 1 << 32
HALF = 1 << 31
FIELDS = ("seq", "t_first_in", "t_last_in", "t_first_out", "t_last_out")
METRICS = ("period", "period_core", "core_gap", "export", "service", "latency")


def delta(new, old):
    return (new - old) % MOD


def decode(raw, sanity=HALF):
    if len(raw) != RECORD.size or raw[:2] != SYNC:
        raise ValueError("invalid sync or record length")
    values = RECORD.unpack(raw)[1:]
    result = dict(zip(FIELDS, values))
    _, fi, li, fo, lo = values
    span, gap, export = delta(li, fi), delta(fo, li), delta(lo, fo)
    latency = delta(lo, fi)
    if any(x >= sanity for x in (span, gap, export, latency, delta(lo, li))):
        raise ValueError("timestamp order or sanity limit")
    if span + gap + export != latency:
        raise ValueError("timestamp order across wrap")
    result["cycles"] = {
        "core_gap": gap, "export": export, "service": delta(lo, li),
        "latency": latency, "input_span": span, "output_span": export,
    }
    return result


def sequence_ok(current, previous, mode="throughput"):
    distance = delta(current["seq"], previous["seq"])
    return distance == 1 if mode == "normal" else 0 < distance < HALF


class ReportStream:
    """Only acquire lock after two adjacent valid records; retain partial tails.

    Once locked a final complete record can be emitted without a successor.
    Any broken record drops lock; the next acquisition starts a new segment.
    """
    def __init__(self, sanity=HALF):
        self.sanity = sanity
        self.buffer = bytearray()
        self.locked = False
        self.previous = None
        self.segment = -1
        self.discarded = Counter()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        output = []
        while len(self.buffer) >= RECORD.size:
            if self.buffer[:2] != SYNC:
                self.discarded["garbage_bytes"] += 1
                del self.buffer[0]
                self.locked = False
                continue
            try:
                current = decode(bytes(self.buffer[:22]), self.sanity)
                if self.locked and not sequence_ok(current, self.previous):
                    raise ValueError("duplicate or nonmonotonic sequence")
            except ValueError as exc:
                self.discarded[str(exc)] += 1
                del self.buffer[0]
                self.locked = False
                continue
            if not self.locked:
                if len(self.buffer) < 44:
                    break
                try:
                    following = decode(bytes(self.buffer[22:44]), self.sanity)
                    if not sequence_ok(following, current):
                        raise ValueError("duplicate or nonmonotonic sequence")
                except ValueError as exc:
                    self.discarded["sync confirmation: " + str(exc)] += 1
                    del self.buffer[0]
                    continue
                self.segment += 1
                self.locked = True
            raw = bytes(self.buffer[:22])
            del self.buffer[:22]
            current["segment"] = self.segment
            self.previous = current
            output.append((raw, current))
        return output

    def finish(self):
        if self.buffer:
            self.discarded["trailing_or_unconfirmed_bytes"] += len(self.buffer)
            self.buffer.clear()


def distribution(values):
    if not values:
        return None
    ordered = sorted(values)
    # Nearest rank: x[ceil(p*n)-1], not interpolated quantiles.
    def percentile(p):
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)]
    return {"min": ordered[0], "mean": statistics.mean(ordered), "max": ordered[-1],
            "std": statistics.pstdev(ordered), "p50": percentile(.5),
            "p95": percentile(.95), "p99": percentile(.99)}


def analyze(records, n, fclk, mode, duration=None):
    reports, pairs, warnings = [], [], []
    previous = None
    wraps = 0
    seq_pairs = 0
    seq_frames = 0
    skipped = 0
    for raw, segment in records:
        try:
            current = decode(bytes.fromhex(raw))
        except ValueError as exc:
            warnings.append("Rejected record: " + str(exc))
            previous = None
            continue
        current["segment"] = segment
        current["us"] = {key: value * 1e6 / fclk for key, value in current["cycles"].items()}
        reports.append(current)
        if previous is not None and previous["segment"] == segment:
            if not sequence_ok(current, previous, mode):
                warnings.append("Rejected pair: duplicate or nonmonotonic sequence")
                previous = current
                continue
            dseq = delta(current["seq"], previous["seq"])
            seq_pairs += 1
            seq_frames += dseq
            skipped += dseq - 1
            di = delta(current["t_first_in"], previous["t_first_in"])
            do = delta(current["t_first_out"], previous["t_first_out"])
            if not 0 < di < HALF or not 0 < do < HALF:
                warnings.append("Rejected pair: zero or ambiguous timestamp delta (>=2**31)")
            else:
                period, core = di / dseq, do / dseq
                pairs.append({"dseq": dseq, "period": period, "period_core": core,
                              "input_cycles": di, "core_cycles": do, "skipped": dseq - 1,
                              "frames_per_s": fclk / period, "samples_per_s": n * fclk / period,
                              "samples_per_cycle": n / core, "utilization_percent": n / core * 100})
            if current["seq"] < previous["seq"]:
                wraps += 1
        previous = current
    summary = {key: distribution([p[key] for p in pairs] if key in ("period", "period_core")
                                else [r["cycles"][key] for r in reports]) for key in METRICS}
    summary_us = {key: None if value is None else {k: v * 1e6 / fclk for k, v in value.items()}
                  for key, value in summary.items()}
    frames = sum(p["dseq"] for p in pairs)
    input_cycles = sum(p["input_cycles"] for p in pairs)
    core_cycles = sum(p["core_cycles"] for p in pairs)
    throughput = {
        "samples_per_s": n * fclk * frames / input_cycles if input_cycles else None,
        "frames_per_s": fclk * frames / input_cycles if input_cycles else None,
        "samples_per_cycle": n * frames / core_cycles if core_cycles else None,
        "utilization_percent": 100 * n * frames / core_cycles if core_cycles else None,
    }
    # Every disconnected segment contributes its own observed baseline only;
    # unknown gaps between segments must not be counted as measured frames.
    baselines = len(reports) - seq_pairs
    counters = {"reports_received": len(reports), "frames_accounted": baselines + seq_frames,
                "frames_skipped": skipped, "seq_wraps": wraps,
                "report_rate_hz": len(reports) / duration if duration and duration > 0 else None}
    return {"reports": reports, "pairs": pairs, "summary": summary, "summary_us": summary_us,
            "throughput": throughput, "counters": counters, "warnings": warnings}
