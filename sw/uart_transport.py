"""Bounded, one-frame-at-a-time UART transactions (8N1, no flow control)."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable


class ExchangeError(RuntimeError):
    def __init__(self, message: str, received: bytes = b"", sent: int = 0):
        super().__init__(message + "; reset the FPGA before retrying an incomplete transaction")
        self.received = received
        self.sent = sent


@dataclass(frozen=True)
class Exchange:
    received: bytes
    write_seconds: float
    elapsed_seconds: float

    def metrics(self, samples: int, payload_bytes: int) -> dict:
        return {
            "write_seconds": self.write_seconds,
            "roundtrip_seconds": self.elapsed_seconds,
            "roundtrip_samples_per_second": samples / self.elapsed_seconds,
            "roundtrip_payload_bytes_per_second": 2 * payload_bytes / self.elapsed_seconds,
        }


def exchange_frame(
    port, payload: bytes, timeout: float,
    clock: Callable[[], float] = time.perf_counter,
) -> Exchange:
    """Write all bytes, then read exactly one response of the same length.

One deadline covers both directions. No flush() is used: it has no portable
deadline in pySerial; receiving a complete response proves transmission ended.
Buffered response bytes may already arrive while the final write completes.
"""
    if not payload or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("payload must be nonempty and timeout must be finite and positive")
    if port.in_waiting:
        raise ExchangeError("unexpected buffered UART bytes before frame")
    start = clock()
    deadline = start + timeout
    sent = 0
    received = bytearray()
    old_timeout, old_write_timeout = port.timeout, port.write_timeout
    try:
        while sent < len(payload):
            remaining = deadline - clock()
            if remaining <= 0:
                raise ExchangeError(f"TX timeout: sent {sent}/{len(payload)} bytes", sent=sent)
            port.write_timeout = remaining
            count = port.write(payload[sent:])
            if count is None or count < 0 or count > len(payload) - sent:
                raise ExchangeError("invalid serial write result", sent=sent)
            sent += count
            if not count:
                time.sleep(min(0.001, remaining))
        written = clock()
        while len(received) < len(payload):
            remaining = deadline - clock()
            if remaining <= 0:
                raise ExchangeError(
                    f"RX timeout: received {len(received)}/{len(payload)} bytes",
                    bytes(received), sent,
                )
            port.timeout = min(0.1, remaining)
            chunk = port.read(len(payload) - len(received))
            received.extend(chunk)
        elapsed = clock() - start
        if port.in_waiting:
            raise ExchangeError("extra UART bytes after frame", bytes(received), sent)
        return Exchange(bytes(received), written - start, elapsed)
    except (OSError, ValueError) as exc:
        raise ExchangeError(str(exc), bytes(received), sent) from exc
    finally:
        port.timeout, port.write_timeout = old_timeout, old_write_timeout


def open_port(name: str, baudrate: int, timeout: float):
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("UART requires pySerial: python -m pip install -r sw/requirements.txt") from exc
    return serial.serial_for_url(
        name, baudrate=baudrate, bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
        timeout=min(timeout, 0.1), write_timeout=timeout,
        xonxoff=False, rtscts=False, dsrdtr=False,
    )


def list_ports() -> list[dict]:
    try:
        from serial.tools import list_ports as serial_ports
    except ImportError as exc:
        raise RuntimeError("port listing requires pySerial: python -m pip install -r sw/requirements.txt") from exc
    return [{"port": item.device, "description": item.description} for item in sorted(serial_ports.comports())]
