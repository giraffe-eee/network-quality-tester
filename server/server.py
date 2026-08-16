#!/usr/bin/env python3
"""Network quality measurement endpoint for competitive-game traffic."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import secrets
import socket
import socketserver
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any


VERSION = "3.0.0"
UDP_MAGIC = b"NQUD"
TCP_MAGIC = b"NQTC"
HEADER = struct.Struct("!4s16sIQ")
HEADER_SIZE = HEADER.size
HEAVY_GAME_PROFILE = "heavy-game-v3"
UDP_LOAD_SEQUENCE_FLAG = 1 << 31
UDP_SEQUENCE_MASK = UDP_LOAD_SEQUENCE_FLAG - 1
DEFAULT_CONTROL_PORT = 37820
DEFAULT_UDP_PORT = 37821
DEFAULT_TCP_PORT = 37822
MAX_DURATION_SECONDS = 120
MAX_RATE = 200
MAX_PAYLOAD_SIZE = 1024
MIN_UDP_PAYLOAD_SIZE = 40
MAX_TARGET_MBPS = 5.0
SESSION_TTL_SECONDS = 480


def now_ns() -> int:
    return time.monotonic_ns()


def json_response(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def recv_line(sock: socket.socket, limit: int = 65536) -> bytes:
    data = bytearray()
    while len(data) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        newline = chunk.find(b"\n")
        if newline >= 0:
            data.extend(chunk[:newline])
            break
        data.extend(chunk)
    if len(data) >= limit:
        raise ValueError("request is too large")
    return bytes(data)


def recv_exact(sock: socket.socket, size: int) -> bytes | None:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


@dataclass
class Session:
    session_id: bytes
    client_ip: str
    duration_seconds: int
    rate: int
    payload_size: int
    profile: str = "legacy-v2"
    udp_min_payload_size: int = MIN_UDP_PAYLOAD_SIZE
    udp_max_payload_size: int = MAX_PAYLOAD_SIZE
    target_mbps: float = 0.0
    udp_echo_payload: bool = False
    created_at: float = field(default_factory=time.monotonic)
    udp_sequences: set[int] = field(default_factory=set)
    udp_load_sequences: set[int] = field(default_factory=set)
    tcp_sequences: set[int] = field(default_factory=set)
    udp_duplicates: int = 0
    udp_load_duplicates: int = 0
    tcp_duplicates: int = 0
    finished: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def is_expired(self) -> bool:
        return time.monotonic() - self.created_at > SESSION_TTL_SECONDS

    def accepts(self, address: tuple[str, int]) -> bool:
        return address[0] == self.client_ip and not self.is_expired()

    def accepts_udp_payload(self, payload_size: int) -> bool:
        if self.udp_echo_payload:
            return self.udp_min_payload_size <= payload_size <= self.udp_max_payload_size
        return payload_size == self.payload_size

    def record_udp(self, sequence: int) -> bool:
        is_load = bool(sequence & UDP_LOAD_SEQUENCE_FLAG)
        sequence_id = sequence & UDP_SEQUENCE_MASK if is_load else sequence
        with self.lock:
            sequences = self.udp_load_sequences if is_load else self.udp_sequences
            if sequence_id in sequences:
                if is_load:
                    self.udp_load_duplicates += 1
                else:
                    self.udp_duplicates += 1
                return False
            sequences.add(sequence_id)
            return True

    def record_tcp(self, sequence: int) -> bool:
        with self.lock:
            if sequence in self.tcp_sequences:
                self.tcp_duplicates += 1
                return False
            self.tcp_sequences.add(sequence)
            return True

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "udp_received": len(self.udp_sequences),
                "udp_duplicates": self.udp_duplicates,
                "udp_load_received": len(self.udp_load_sequences),
                "udp_load_duplicates": self.udp_load_duplicates,
                "tcp_received": len(self.tcp_sequences),
                "tcp_duplicates": self.tcp_duplicates,
            }


class ServiceState:
    def __init__(self, udp_port: int, tcp_port: int) -> None:
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.sessions: dict[bytes, Session] = {}
        self.lock = threading.Lock()

    def cleanup(self) -> None:
        with self.lock:
            expired = [key for key, value in self.sessions.items() if value.is_expired()]
            for key in expired:
                del self.sessions[key]

    def create_session(
        self,
        client_ip: str,
        duration: int,
        rate: int,
        payload_size: int,
        *,
        profile: str = "legacy-v2",
        udp_min_payload_size: int = MIN_UDP_PAYLOAD_SIZE,
        udp_max_payload_size: int = MAX_PAYLOAD_SIZE,
        target_mbps: float = 0.0,
        udp_echo_payload: bool = False,
    ) -> Session:
        self.cleanup()
        with self.lock:
            active_for_ip = sum(1 for item in self.sessions.values() if item.client_ip == client_ip)
            if active_for_ip >= 4:
                raise ValueError("too many active sessions from this address")
            session = Session(
                session_id=secrets.token_bytes(16),
                client_ip=client_ip,
                duration_seconds=duration,
                rate=rate,
                payload_size=payload_size,
                profile=profile,
                udp_min_payload_size=udp_min_payload_size,
                udp_max_payload_size=udp_max_payload_size,
                target_mbps=target_mbps,
                udp_echo_payload=udp_echo_payload,
            )
            self.sessions[session.session_id] = session
            return session

    def get_session(self, session_id: bytes) -> Session | None:
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None or session.is_expired():
            return None
        return session


class UDPProbeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        if len(data) < HEADER_SIZE:
            return
        try:
            magic, session_id, sequence, _client_time = HEADER.unpack_from(data)
        except struct.error:
            return
        if magic != UDP_MAGIC:
            return
        session = self.server.state.get_session(session_id)  # type: ignore[attr-defined]
        if session is None or not session.accepts(self.client_address):
            return
        payload = data[HEADER_SIZE:]
        if not session.accepts_udp_payload(len(payload)):
            return
        if not session.record_udp(sequence):
            return
        acknowledgement = HEADER.pack(UDP_MAGIC, session_id, sequence, now_ns())
        if session.udp_echo_payload:
            acknowledgement += payload
        try:
            sock.sendto(acknowledgement, self.client_address)
        except OSError:
            logging.debug("UDP response failed for %s", self.client_address[0])


class UDPServer(socketserver.UDPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: ServiceState) -> None:
        self.state = state
        super().__init__(address, UDPProbeHandler)


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(10)
        client_ip = self.client_address[0]
        try:
            raw = recv_line(self.connection)
            if not raw:
                return
            request = json.loads(raw.decode("utf-8"))
            response = self.dispatch(request, client_ip)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception:
            logging.exception("Control request failed from %s", client_ip)
            response = {"ok": False, "error": "internal server error"}
        try:
            self.wfile.write(json_response(response))
        except OSError:
            logging.debug("Control response failed for %s", client_ip)

    def dispatch(self, request: dict[str, Any], client_ip: str) -> dict[str, Any]:
        operation = request.get("op")
        if operation == "health":
            return {
                "ok": True,
                "service": "network-quality-server",
                "version": VERSION,
                "time_ns": now_ns(),
            }
        if operation == "start":
            duration = int(request.get("duration_seconds", 60))
            rate = int(request.get("rate", 120))
            payload_size = int(request.get("payload_size", 256))
            profile = str(request.get("profile", "legacy-v2"))
            if not 5 <= duration <= MAX_DURATION_SECONDS:
                raise ValueError(f"duration must be between 5 and {MAX_DURATION_SECONDS} seconds")
            if not 5 <= rate <= MAX_RATE:
                raise ValueError(f"rate must be between 5 and {MAX_RATE} packets per second")
            if not 64 <= payload_size <= MAX_PAYLOAD_SIZE:
                raise ValueError(f"payload_size must be between 64 and {MAX_PAYLOAD_SIZE} bytes")
            if profile == HEAVY_GAME_PROFILE:
                udp_min_payload_size = int(request.get("udp_min_payload_size", MIN_UDP_PAYLOAD_SIZE))
                udp_max_payload_size = int(request.get("udp_max_payload_size", MAX_PAYLOAD_SIZE))
                target_mbps = float(request.get("target_mbps", 3.0))
                if not MIN_UDP_PAYLOAD_SIZE <= udp_min_payload_size <= udp_max_payload_size <= MAX_PAYLOAD_SIZE:
                    raise ValueError(
                        f"UDP payload range must be between {MIN_UDP_PAYLOAD_SIZE} and {MAX_PAYLOAD_SIZE} bytes"
                    )
                if not math.isfinite(target_mbps) or not 0.1 <= target_mbps <= MAX_TARGET_MBPS:
                    raise ValueError(f"target_mbps must be between 0.1 and {MAX_TARGET_MBPS}")
                udp_echo_payload = True
            elif profile == "legacy-v2":
                udp_min_payload_size = payload_size
                udp_max_payload_size = payload_size
                target_mbps = 0.0
                udp_echo_payload = False
            else:
                raise ValueError("unsupported test profile")
            session = self.server.state.create_session(  # type: ignore[attr-defined]
                client_ip,
                duration,
                rate,
                payload_size,
                profile=profile,
                udp_min_payload_size=udp_min_payload_size,
                udp_max_payload_size=udp_max_payload_size,
                target_mbps=target_mbps,
                udp_echo_payload=udp_echo_payload,
            )
            return {
                "ok": True,
                "version": VERSION,
                "profile": session.profile,
                "session_id": session.session_id.hex(),
                "duration_seconds": duration,
                "rate": rate,
                "payload_size": payload_size,
                "udp_min_payload_size": session.udp_min_payload_size,
                "udp_max_payload_size": session.udp_max_payload_size,
                "target_mbps": session.target_mbps,
                "udp_echo_payload": session.udp_echo_payload,
                "udp_port": self.server.state.udp_port,  # type: ignore[attr-defined]
                "tcp_port": self.server.state.tcp_port,  # type: ignore[attr-defined]
            }
        if operation == "finish":
            session_id = bytes.fromhex(str(request.get("session_id", "")))
            session = self.server.state.get_session(session_id)  # type: ignore[attr-defined]
            if session is None or not session.accepts((client_ip, 0)):
                raise ValueError("invalid or expired session")
            with session.lock:
                session.finished = True
            snapshot = session.snapshot()
            snapshot.update({"ok": True, "session_id": session_id.hex(), "profile": session.profile})
            return snapshot
        raise ValueError("unknown operation")


class ControlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: ServiceState) -> None:
        self.state = state
        super().__init__(address, ControlHandler)


class TCPEchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(10)
        client_ip = self.client_address[0]
        try:
            first_header = recv_exact(self.request, HEADER_SIZE)
            if first_header is None:
                return
            magic, session_id, sequence, _client_time = HEADER.unpack(first_header)
            if magic != TCP_MAGIC:
                return
            session = self.server.state.get_session(session_id)  # type: ignore[attr-defined]
            if session is None or not session.accepts(self.client_address):
                return
            payload = recv_exact(self.request, session.payload_size)
            if payload is None:
                return
            self.echo(session, session_id, sequence, payload)
            while True:
                header = recv_exact(self.request, HEADER_SIZE)
                if header is None:
                    break
                magic, session_id, sequence, _client_time = HEADER.unpack(header)
                if magic != TCP_MAGIC or session_id != session.session_id:
                    break
                payload = recv_exact(self.request, session.payload_size)
                if payload is None:
                    break
                self.echo(session, session_id, sequence, payload)
        except (OSError, struct.error):
            logging.debug("TCP echo ended for %s", client_ip)

    def echo(self, session: Session, session_id: bytes, sequence: int, payload: bytes) -> None:
        session.record_tcp(sequence)
        response = HEADER.pack(TCP_MAGIC, session_id, sequence, now_ns()) + payload
        self.request.sendall(response)


class TCPEchoServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: ServiceState) -> None:
        self.state = state
        super().__init__(address, TCPEchoHandler)


def run(args: argparse.Namespace) -> None:
    state = ServiceState(args.udp_port, args.tcp_port)
    control = ControlServer((args.host, args.control_port), state)
    udp = UDPServer((args.host, args.udp_port), state)
    tcp = TCPEchoServer((args.host, args.tcp_port), state)
    logging.info(
        "listening control=tcp/%d udp=udp/%d tcp_echo=tcp/%d",
        args.control_port,
        args.udp_port,
        args.tcp_port,
    )
    servers = (control, udp, tcp)
    threads = [
        threading.Thread(target=server.serve_forever, name=f"listener-{index}", daemon=True)
        for index, server in enumerate(servers)
    ]
    for thread in threads:
        thread.start()
    try:
        while True:
            time.sleep(5)
            state.cleanup()
    except KeyboardInterrupt:
        logging.info("shutting down")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Network quality UDP/TCP measurement endpoint")
    parser.add_argument("--host", default=os.getenv("NQ_HOST", "0.0.0.0"))
    parser.add_argument("--control-port", type=int, default=int(os.getenv("NQ_CONTROL_PORT", DEFAULT_CONTROL_PORT)))
    parser.add_argument("--udp-port", type=int, default=int(os.getenv("NQ_UDP_PORT", DEFAULT_UDP_PORT)))
    parser.add_argument("--tcp-port", type=int, default=int(os.getenv("NQ_TCP_PORT", DEFAULT_TCP_PORT)))
    parser.add_argument("--log-level", default=os.getenv("NQ_LOG_LEVEL", "INFO"))
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    logging.basicConfig(
        level=getattr(logging, arguments.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    run(arguments)
