"""零依赖备用 HTTP 服务器（仅用标准库）。

用途
----
FastAPI 是推荐部署方式（见 :mod:`smartrail.api.app`），但它需要联网安装依赖。
在没有外网的内网/离线环境里，可以用本模块起一个**接口完全同构**的服务，
方便验证前端、演示与联调：

    python -m smartrail.api.stdlib_server --port 8000

接口与 FastAPI 版一一对应：
    GET  /                     可视化前端
    GET  /api/snapshot         余票与编组快照
    GET  /api/config           权重与阈值
    POST /api/book             购票决策
    POST /api/scenario         预置场景演练
    POST /api/credit           信用分闭环
    POST /api/release          释放座位
    POST /api/reset            重置环境

生产部署请使用 FastAPI 版本（自带 OpenAPI 文档、并发与校验能力）。
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..credit import CreditLedger
from ..engine import SeatEngine
from . import service

STATIC_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_BODY_BYTES = 1 << 20  # 1 MiB，防止超大请求体打满内存


class DemoState:
    """线程安全的演示状态（引擎 + 锁）。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.engine: SeatEngine = service.create_engine("crh16", CreditLedger())

    def reset(self, formation: str, fill: float = 0.0, seed: int = 7) -> SeatEngine:
        with self.lock:
            self.engine = service.reset_engine(
                self.engine, formation=formation, fill=fill, seed=seed
            )
            return self.engine


STATE = DemoState()


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartRailStdlib/1.0"
    protocol_version = "HTTP/1.1"

    # -- 工具 ------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"请求体不是合法 JSON：{error}") from error
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # -- 路由 ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (标准库命名)
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send_page("index.html")
            elif path == "/booking":
                self._send_page("booking.html")
            elif path == "/acceptance":
                self._send_page("acceptance.html")
            elif path == "/ticket-first":
                self._send_page("ticket-first.html")
            elif path == "/api/snapshot":
                with STATE.lock:
                    self._send_json(200, STATE.engine.snapshot())
            elif path == "/api/config":
                with STATE.lock:
                    self._send_json(200, service.config_payload(STATE.engine))
            elif path == "/api/training-curve":
                self._send_json(200, service.training_curve())
            elif path == "/api/orders/types":
                self._send_json(200, self._order_types({})[1])
            elif path.startswith("/api/scenario/passengers/"):
                name = path.rsplit("/", 1)[-1]
                self._send_json(200, service.scenario_passengers(name))
            elif path.startswith("/api/credit/"):
                passenger_id = path.rsplit("/", 1)[-1]
                with STATE.lock:
                    self._send_json(200, STATE.engine.credit.get(passenger_id).to_dict())
            else:
                self._send_json(404, {"detail": f"未知路径 {path}"})
        except Exception as error:  # pragma: no cover - 兜底
            self._send_json(500, {"detail": f"服务内部错误：{error}"})

    def _send_page(self, name: str) -> None:
        page = STATIC_DIR / name
        if not page.exists():
            self._send(404, b"page missing", "text/plain; charset=utf-8")
            return
        self._send(200, page.read_bytes(), "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except ValueError as error:
            self._send_json(422, {"detail": str(error)})
            return
        handlers: dict[str, Callable[[dict[str, Any]], tuple[int, Any]]] = {
            "/api/book": self._book,
            "/api/scenario": self._scenario,
            "/api/credit": self._credit,
            "/api/release": self._release,
            "/api/reset": self._reset,
            "/api/compare": self._compare,
            "/api/simulate/concurrent": self._simulate,
            "/api/orders/submit": self._submit_orders,
            "/api/orders/types": self._order_types,
        }
        handler = handlers.get(path)
        if handler is None:
            self._send_json(404, {"detail": f"未知路径 {path}"})
            return
        try:
            status, body = handler(payload)
        except KeyError as error:
            status, body = 404, {"detail": str(error.args[0])}
        except ValueError as error:
            status, body = 422, {"detail": str(error)}
        except Exception as error:  # pragma: no cover - 兜底
            status, body = 500, {"detail": f"服务内部错误：{error}"}
        self._send_json(status, body)

    # -- 具体动作 --------------------------------------------------------
    def _book(self, payload: dict[str, Any]) -> tuple[int, Any]:
        with STATE.lock:
            outcome = service.book_order(STATE.engine, payload)
        return outcome["status"], outcome["body"]

    def _scenario(self, payload: dict[str, Any]) -> tuple[int, Any]:
        name = str(payload.get("scenario") or "")
        with STATE.lock:
            return 200, service.run_scenario(STATE.engine, name, payload)

    def _credit(self, payload: dict[str, Any]) -> tuple[int, Any]:
        with STATE.lock:
            return 200, service.update_credit(
                STATE.engine, str(payload["passenger_id"]), str(payload.get("action", ""))
            )

    def _release(self, payload: dict[str, Any]) -> tuple[int, Any]:
        with STATE.lock:
            released = STATE.engine.release(str(payload["order_id"]))
            return 200, {"order_id": payload["order_id"], "released": released, **STATE.engine.snapshot()}

    def _reset(self, payload: dict[str, Any]) -> tuple[int, Any]:
        engine = STATE.reset(
            str(payload.get("formation") or "crh16"),
            fill=float(payload.get("fill") or 0.0),
            seed=int(payload.get("seed") or 7),
        )
        return 200, {
            "reset": True,
            "formation": str(payload.get("formation") or "crh16"),
            "fill": float(payload.get("fill") or 0.0),
            "train_code": engine.formation.train_code,
            **engine.snapshot(),
        }

    def _compare(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """V1 / V2 / V3 并排对比（验收台数据源）。"""
        return 200, service.compare_solvers(payload)

    def _simulate(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """并发订单模拟：任意数量 × 任意人群类型一次性压给引擎。"""
        return 200, service.concurrent_simulation(payload)

    def _submit_orders(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """批量提交**用户手工构造**的订单，逐单给出满足情况与未满足原因。"""
        return 200, service.submit_orders(payload)

    def _order_types(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """乘客类型清单（供前端构造订单时选择）。"""
        from ..feasibility import OUTCOME_LEVELS, feasibility_catalog
        from ..v3.archetypes import archetype_catalog

        return 200, {
            "archetypes": archetype_catalog(),
            "outcome_levels": OUTCOME_LEVELS,
            "feasibility_codes": feasibility_catalog(),
        }


def serve(host: str = "127.0.0.1", port: int = 8000, verbose: bool = False) -> ThreadingHTTPServer:
    """启动服务器（返回实例，便于测试里 shutdown）。"""
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    return httpd


def main() -> None:  # pragma: no cover - 手工运行入口
    parser = argparse.ArgumentParser(description="SmartRail-Seat-Engine 零依赖备用服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    httpd = serve(args.host, args.port, verbose=not args.quiet)
    print(f"SmartRail-Seat-Engine（标准库服务器）已启动：http://{args.host}:{args.port}/")
    print("提示：生产部署请使用 FastAPI 版本：python -m smartrail.api.app")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()
