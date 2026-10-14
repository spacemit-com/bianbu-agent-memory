from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .client import DEFAULT_SOCKET, MemoryClient
from .dplus import DPlusProjector, PROVIDER_NAME, UpstreamMem0Provider


class ServiceState:
    def __init__(
        self,
        client: MemoryClient,
        provider: UpstreamMem0Provider,
        projector: DPlusProjector,
    ) -> None:
        self.client = client
        self.provider = provider
        self.projector = projector
        self.stop = threading.Event()
        self.projection_lock = threading.RLock()
        self.last_worker_error: str | None = None
        self.last_job_at: float | None = None

    def worker_loop(self, poll_seconds: float) -> None:
        while not self.stop.is_set():
            try:
                with self.projection_lock:
                    processed = self.projector.process_one()
                if processed:
                    self.last_worker_error = None
                    self.last_job_at = time.time()
                    continue
            except Exception as error:
                self.last_worker_error = f"{type(error).__name__}: {error}"[:2000]
            self.stop.wait(poll_seconds)

    def health(self) -> dict[str, Any]:
        try:
            projection = self.client.projection.status(provider=PROVIDER_NAME)
            memoryd = self.client.health()
        except Exception as error:
            return {
                "status": "degraded",
                "error": f"{type(error).__name__}: {error}",
                "circuit": self.provider.breaker.state,
            }
        return {
            "status": (
                "ok"
                if self.provider.breaker.state == "closed"
                and not self.last_worker_error
                else "degraded"
            ),
            "provider": PROVIDER_NAME,
            "circuit": self.provider.breaker.state,
            "projection": projection,
            "memoryd": memoryd,
            "last_worker_error": self.last_worker_error,
            "last_job_at": self.last_job_at,
        }


class Handler(BaseHTTPRequestHandler):
    server: "DPlusHTTPServer"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, self.server.state.health())
            return
        if self.path == "/v1/status":
            self._json(200, self.server.state.health())
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        try:
            payload = self._payload()
            if self.path == "/v1/search":
                query = str(payload.get("query") or "").strip()
                top_k = max(1, min(int(payload.get("top_k") or 5), 20))
                if not query:
                    self._json(400, {"error": "query_required"})
                    return
                with self.server.state.projection_lock:
                    result = self.server.state.projector.search_canonical(query, top_k)
                self._json(200, {"results": result, "provider": PROVIDER_NAME})
                return
            if self.path == "/v1/rebuild":
                limit = max(1, min(int(payload.get("limit") or 100), 100))
                with self.server.state.projection_lock:
                    result = self.server.state.projector.rebuild(limit)
                self._json(200, result)
                return
            if self.path == "/v1/clear":
                with self.server.state.projection_lock:
                    result = self.server.state.projector.clear_scope()
                self._json(200, result)
                return
            if self.path == "/v1/prune":
                with self.server.state.projection_lock:
                    result = self.server.state.projector.prune_orphans()
                self._json(200, result)
                return
            self._json(404, {"error": "not_found"})
        except Exception as error:
            self._json(
                503,
                {
                    "error": "provider_unavailable",
                    "detail": f"{type(error).__name__}: {error}"[:2000],
                },
            )

    def _payload(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid content length") from None
        if length < 0 or length > 128 * 1024:
            raise ValueError("request body is too large")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class DPlusHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: ServiceState) -> None:
        super().__init__(address, Handler)
        self.state = state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bianbu AgentMemory D+ Mem0 projection and semantic recall sidecar"
    )
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18920)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("D+ sidecar may only bind to loopback")

    client = MemoryClient.connect(args.socket, timeout=5.0)
    provider = UpstreamMem0Provider()
    projector = DPlusProjector(client, provider)
    if args.rebuild:
        print(json.dumps(projector.rebuild(), ensure_ascii=False))
        client.close()
        return 0

    state = ServiceState(client, provider, projector)
    server = DPlusHTTPServer((args.host, args.port), state)
    worker = threading.Thread(
        target=state.worker_loop,
        args=(max(0.1, args.poll_seconds),),
        name="agent-memory-dplus-projector",
        daemon=True,
    )
    worker.start()

    def stop(*_: object) -> None:
        state.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        state.stop.set()
        worker.join(timeout=5.0)
        server.server_close()
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
