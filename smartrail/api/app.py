"""FastAPI 服务层：把分配引擎暴露为 HTTP 接口，并提供可视化座位图。

启动：
    python -m smartrail.api.app            # 等价于 uvicorn smartrail.api.app:app
    uvicorn smartrail.api.app:app --reload
然后访问 http://127.0.0.1:8000/ 查看座位图与场景演练。

业务逻辑全部在 :mod:`smartrail.api.service`（不依赖 Web 框架）；
本模块只负责路由、参数校验与错误码映射。
若运行环境没有 FastAPI，可用等价的内置服务器：
    python -m smartrail.api.stdlib_server
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..credit import CreditLedger
from ..engine import SeatEngine
from ..models import BondType, DeclaredBehavior, RelationType, SupportNeed, TicketType
from ..router import AllocationMode
from . import service

STATIC_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(
    title="SmartRail-Seat-Engine",
    description="智能铁路座位协同分配引擎 —— 多目标动态资源调度与图匹配（学术演示）",
    version="1.0.0",
)

# 演示引擎：16 节编组 + 政务资格模拟数据 + 内存信用账本
engine: SeatEngine = service.create_engine("crh16", CreditLedger())


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class PassengerIn(BaseModel):
    passenger_id: str = Field(..., description="乘客唯一标识（演示用，勿填真实身份信息）")
    name: str = ""
    age: int = 35
    ticket_type: TicketType = TicketType.ADULT
    support_needs: list[SupportNeed] = Field(default_factory=list)
    declared_behavior: DeclaredBehavior = DeclaredBehavior.UNKNOWN
    preference_aisle: bool = False
    preference_window: bool = False
    quietness_score: float = 100.0


class BondIn(BaseModel):
    a: str
    b: str
    bond: BondType = BondType.STRONG


class BookRequest(BaseModel):
    order_id: str = "O-WEB"
    relation: RelationType = RelationType.SOLO
    passengers: list[PassengerIn]
    bonds: list[BondIn] = Field(default_factory=list)
    mode: AllocationMode | None = Field(
        default=None, description="留空则交由决策路由按余票率自动选择"
    )
    availability_ratio: float | None = Field(
        default=None, description="覆盖路由用的余票率（用于演示不同模式）"
    )
    concurrency: int = 0
    chosen_seats: dict[str, str] | None = Field(
        default=None, description="模式一的自选座位：乘客 ID -> 座位 ID"
    )


class ScenarioRequest(BaseModel):
    scenario: str = Field(..., description=f"可选：{', '.join(service.scenario_names())}")
    mode: AllocationMode | None = None
    availability_ratio: float | None = None
    concurrency: int = 0


class CreditRequest(BaseModel):
    passenger_id: str
    action: str = Field(..., description="complaint | commendation")


class ReleaseRequest(BaseModel):
    order_id: str


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    """可视化前端（单文件 SPA）。"""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>SmartRail-Seat-Engine</h1><p>前端资源缺失：smartrail/web/index.html</p>")
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


@app.get("/acceptance", response_class=HTMLResponse, include_in_schema=False)
def acceptance() -> HTMLResponse:
    """V1/V2/V3 验收台（三版本对比 + 座位图 + 学习曲线）。"""
    page = STATIC_DIR / "acceptance.html"
    if not page.exists():
        return HTMLResponse("<h1>验收台缺失</h1><p>缺少 smartrail/web/acceptance.html</p>")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/ticket-first", response_class=HTMLResponse, include_in_schema=False)
def ticket_first() -> HTMLResponse:
    """出票优先策略演示：没有相邻座位时如何出票并交办现场处理。"""
    page = STATIC_DIR / "ticket-first.html"
    if not page.exists():
        return HTMLResponse("<h1>页面缺失</h1><p>缺少 smartrail/web/ticket-first.html</p>")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/api/scenario/passengers/{name}")
def get_scenario_passengers(name: str) -> dict[str, Any]:
    """把预置场景导出为可直接填表的数据（前端不重复维护乘客定义）。"""
    try:
        return service.scenario_passengers(name)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error.args[0])) from error


class ConcurrentRequest(BaseModel):
    presets: dict[str, int] | list[str] | None = Field(
        default=None, description="人群类型 -> 数量；缺省表示全部类型各 1 单"
    )
    fill: float = Field(default=0.0, ge=0.0, le=0.95)
    seed: int = 7
    solver: str = "v1_heuristic"
    formation: str = "crh16"


@app.post("/api/simulate/concurrent")
def concurrent_simulation(payload: ConcurrentRequest) -> dict[str, Any]:
    """并发订单模拟：任意数量 × 任意人群类型，一次性压给引擎。"""
    return service.concurrent_simulation(payload.model_dump())


class CompareRequest(BaseModel):
    steps: int = Field(default=40, ge=1, le=500)
    seed: int = 7
    fill: float = Field(default=0.0, ge=0.0, le=0.95)
    complaint_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    policies: list[str] | None = None
    time_budget_ms: float | None = None


@app.post("/api/compare")
def compare(payload: CompareRequest) -> dict[str, Any]:
    """V1 / V2 / V3 在同一订单流上的并排对比（验收台数据源）。"""
    return service.compare_solvers(payload.model_dump())


@app.get("/api/training-curve")
def get_training_curve() -> dict[str, Any]:
    """V3 的 PPO 学习曲线（训练产物）。"""
    return service.training_curve()


@app.get("/api/snapshot")
def snapshot() -> dict[str, Any]:
    """余票与编组快照（座位图数据源）。"""
    return engine.snapshot()


@app.get("/api/config")
def config() -> dict[str, Any]:
    """代价函数权重、阈值与可选场景。"""
    return service.config_payload(engine)


@app.post("/api/book")
def book(payload: BookRequest) -> dict[str, Any]:
    """提交购票请求，返回分配方案、代价明细与数学解释。"""
    try:
        outcome = service.book_order(engine, payload.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if outcome["status"] != 200:
        raise HTTPException(status_code=outcome["status"], detail=outcome["body"])
    return outcome["body"]


@app.post("/api/scenario")
def run_scenario(payload: ScenarioRequest) -> dict[str, Any]:
    """一键演练预置场景（带娃家庭 / 轮椅 / 视障 / 孕晚期 / 多代家庭 …）。"""
    try:
        return service.run_scenario(engine, payload.scenario, payload.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error.args[0])) from error


@app.post("/api/credit")
def update_credit(payload: CreditRequest) -> dict[str, Any]:
    """更新静音信用分（乘务员端投诉 / 表扬闭环）。"""
    try:
        return service.update_credit(engine, payload.passenger_id, payload.action)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/credit/{passenger_id}")
def get_credit(passenger_id: str) -> dict[str, Any]:
    return engine.credit.get(passenger_id).to_dict()


@app.post("/api/release")
def release(payload: ReleaseRequest) -> dict[str, Any]:
    """退票 / 释放座位。"""
    count = engine.release(payload.order_id)
    return {"order_id": payload.order_id, "released": count, **engine.snapshot()}


@app.post("/api/reset")
def reset(formation: str = "crh16") -> dict[str, Any]:
    """重置演示环境（保留信用账本）。"""
    global engine
    engine = service.create_engine(formation, engine.credit)
    return {"reset": True, "train_code": engine.formation.train_code, **engine.snapshot()}


def main() -> None:  # pragma: no cover - 手工启动入口
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":  # pragma: no cover
    main()
