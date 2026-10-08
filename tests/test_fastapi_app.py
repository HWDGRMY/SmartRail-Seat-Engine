"""FastAPI 版本的真实端到端验证（需要 fastapi + httpx）。

运行：
    pip install fastapi "uvicorn[standard]" httpx pydantic
    python tests/test_fastapi_app.py

没有外网/装不上 fastapi 时（本项目开发环境就是如此，pip 会被限速到无法完成），
可以把 fastapi 的 wheel 解压到任意目录并用 PYTHONPATH 指过去——
前提是该解释器已经有 fastapi 的依赖（pydantic / starlette / anyio / httpx）：

    pip download --no-deps -d /tmp/wheels fastapi annotated-doc
    python -c "import zipfile,glob;[zipfile.ZipFile(w).extractall('vendor') for w in glob.glob('/tmp/wheels/*.whl')]"
    PYTHONPATH=vendor python tests/test_fastapi_app.py

本文件用 ``httpx.AsyncClient(transport=ASGITransport(app))`` 直接驱动 ASGI 应用
（无需监听端口），因此同时覆盖三件事：FastAPI 路由注册、Pydantic 请求校验、
响应体契约。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import httpx
    from fastapi import FastAPI
except ImportError as error:  # pragma: no cover - 环境缺依赖时给出清晰指引
    raise SystemExit(
        f"跳过 FastAPI 端到端测试：缺少依赖（{error}）。\n"
        "安装方式：pip install fastapi 'uvicorn[standard]' httpx pydantic\n"
        "或把 fastapi 解压目录加入 PYTHONPATH 后重跑本文件（见模块文档）。"
    )

from smartrail.api.app import app  # noqa: E402

FAILURES: list[str] = []
UNDER_PYTEST = "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  PASS  {message}")
        return
    print(f"  FAIL  {message}")
    FAILURES.append(message)
    if UNDER_PYTEST:
        raise AssertionError(message)


async def run_all(client: httpx.AsyncClient) -> None:
    await scenario_endpoints(client)
    await pydantic_validation(client)
    await book_modes(client)
    await credit_release_reset(client)


async def openapi_and_routes(client: httpx.AsyncClient) -> None:
    print("[fastapi] 应用与路由注册")
    check(isinstance(app, FastAPI), "导出的是 FastAPI 实例")
    schema = (await client.get("/openapi.json")).json()
    paths = set(schema["paths"])
    for path in (
        "/api/snapshot",
        "/api/config",
        "/api/book",
        "/api/scenario",
        "/api/credit",
        "/api/credit/{passenger_id}",
        "/api/release",
        "/api/reset",
    ):
        check(path in paths, f"OpenAPI 中注册了 {path}")
    check(schema["info"]["title"] == "SmartRail-Seat-Engine", "OpenAPI 标题正确")

    index = await client.get("/")
    check(index.status_code == 200 and "SmartRail-Seat-Engine" in index.text, "GET / 返回可视化前端")
    snapshot = (await client.get("/api/snapshot")).json()
    # 与编组定义比对，而不是写死数字（各车厢排数不同，见 carriage.crh_16_car_formation）
    from smartrail import crh_16_car_formation

    check(
        snapshot["total_seats"] == len(crh_16_car_formation().seats),
        f"GET /api/snapshot 座位数与编组一致（{snapshot['total_seats']} 座）",
    )
    check("latency" in snapshot and "seats" in snapshot, "快照包含座位图与延迟字段")


async def scenario_endpoints(client: httpx.AsyncClient) -> None:
    print("[fastapi] 场景演练")
    config = (await client.get("/api/config")).json()
    check("family_with_child" in config["scenarios"], f"配置暴露 {len(config['scenarios'])} 个场景")
    response = await client.post("/api/scenario", json={"scenario": "family_with_child"})
    check(response.status_code == 200, "POST /api/scenario 返回 200")
    body = response.json()
    check(len(body["assignments"]) == 3, "带娃家庭 3 人全部分配")
    seats = [a["seat_id"] for a in body["assignments"]]
    check(len(set(seats)) == 3, "一人一座")
    cars = {a["carriage"] for a in body["assignments"]}
    child_car = next(a["carriage"] for a in body["assignments"] if a["passenger_id"] == "C1")
    check(len(cars) == 1 and child_car in cars, "儿童与家长同车厢（Tier 0 守住）")
    check(not [v for v in body["violations"] if v["tier"] == 0], "无 Tier 0 违规")
    check("Tier" in body["explanation"], "返回可解释文本")


async def pydantic_validation(client: httpx.AsyncClient) -> None:
    print("[fastapi] Pydantic 请求校验")
    response = await client.post("/api/scenario", json={"scenario": "no_such_scenario"})
    check(response.status_code == 404, "未知场景 -> 404")
    response = await client.post("/api/book", json={"passengers": []})
    check(response.status_code == 422, "空乘客列表 -> 422")
    response = await client.post(
        "/api/book",
        json={
            "order_id": "O-BAD-ENUM",
            "passengers": [{"passenger_id": "X", "support_needs": ["not_a_need"]}],
        },
    )
    check(response.status_code == 422, "非法 support_needs -> 422（Pydantic 拦截）")
    response = await client.post("/api/book", json={"passengers": [{"passenger_id": "Y"}]})
    check(response.status_code == 200, "最简合法请求 -> 200")


async def book_modes(client: httpx.AsyncClient) -> None:
    print("[fastapi] 模式一自选座位与降级模式")
    await client.post("/api/reset", params={"formation": "crh16"})
    response = await client.post(
        "/api/book",
        json={
            "order_id": "O-FREE-PICK",
            "mode": "free",
            "passengers": [
                {"passenger_id": "A1", "age": 36},
                {"passenger_id": "C1", "age": 5, "ticket_type": "child"},
            ],
            "bonds": [{"a": "A1", "b": "C1", "bond": "mandatory"}],
            "chosen_seats": {"A1": "03车05A", "C1": "03车05B"},
        },
    )
    check(response.status_code == 200, "合法自选座位 -> 200")
    check(len(response.json()["assignments"]) == 2, "自选座位被接受")

    response = await client.post(
        "/api/book",
        json={
            "order_id": "O-FREE-BAD",
            "mode": "free",
            "passengers": [
                {"passenger_id": "A2", "age": 36},
                {"passenger_id": "C2", "age": 5, "ticket_type": "child"},
            ],
            "bonds": [{"a": "A2", "b": "C2", "bond": "mandatory"}],
            "chosen_seats": {"A2": "04车05A", "C2": "09车05B"},
        },
    )
    check(response.status_code == 409, "拆散儿童的自选座位 -> 409（隐性拦截）")
    detail = response.json()["detail"]
    check(any(v["tier"] == 0 for v in detail["violations"]), "拦截理由为 Tier 0")

    # 非法输入必须是可解释的 409，而不是 500（曾因此暴露内部异常）
    response = await client.post(
        "/api/book",
        json={
            "order_id": "O-FREE-NOSEAT",
            "mode": "free",
            "passengers": [{"passenger_id": "N1", "age": 30}],
            "chosen_seats": {"N1": "99车99Z"},
        },
    )
    check(response.status_code == 409, "不存在的座位号 -> 409（而非 500）")
    codes = {v["code"] for v in response.json()["detail"]["violations"]}
    check("V_UNKNOWN_SEAT" in codes, f"给出可解释的座位号错误（{sorted(codes)}）")

    response = await client.post(
        "/api/book",
        json={
            "order_id": "O-FREE-STRANGER",
            "mode": "free",
            "passengers": [{"passenger_id": "N2", "age": 30}],
            "chosen_seats": {"NOT_IN_ORDER": "03车06A"},
        },
    )
    check(response.status_code == 409, "非本单乘客 -> 409")
    codes = {v["code"] for v in response.json()["detail"]["violations"]}
    check("V_UNKNOWN_PASSENGER" in codes, f"给出可解释的乘客错误（{sorted(codes)}）")

    response = await client.post(
        "/api/book",
        json={
            "order_id": "O-FREE-TAKEN",
            "mode": "free",
            "passengers": [{"passenger_id": "N3", "age": 30}],
            "chosen_seats": {"N3": "03车05A"},  # 已被 O-FREE-PICK 占用
        },
    )
    check(response.status_code == 409, "已被占用的座位 -> 409")
    codes = {v["code"] for v in response.json()["detail"]["violations"]}
    check("V_SEAT_TAKEN" in codes, f"给出可解释的占座错误（{sorted(codes)}）")

    response = await client.post(
        "/api/book",
        json={
            "order_id": "O-DEGRADED",
            "mode": "degraded",
            "passengers": [
                {"passenger_id": "D1", "age": 36},
                {"passenger_id": "D2", "age": 5, "ticket_type": "child"},
            ],
            "bonds": [{"a": "D1", "b": "D2", "bond": "mandatory"}],
        },
    )
    body = response.json()
    check(body["solver"] == "greedy", "降级模式使用贪心求解器")
    cars = {a["carriage"] for a in body["assignments"]}
    check(len(cars) == 1, "降级模式下硬绑定仍同车厢")


async def credit_release_reset(client: httpx.AsyncClient) -> None:
    print("[fastapi] 信用闭环 / 释放 / 重置")
    response = await client.post("/api/credit", json={"passenger_id": "Q1", "action": "complaint"})
    body = response.json()
    check(body["score"] == 50.0 and body["blocked"], "投诉后信用分 50 且被屏蔽")
    queried = (await client.get("/api/credit/Q1")).json()
    check(queried["passenger_id"] == "Q1" and queried["blocked"], "GET /api/credit/{id} 可查询")

    released = (await client.post("/api/release", json={"order_id": "O-FREE-PICK"})).json()
    check("released" in released, "POST /api/release 返回释放数量")

    reset = (await client.post("/api/reset", params={"formation": "mini"})).json()
    check(reset["reset"] and reset["total_seats"] < 100, "POST /api/reset 可切换小规模编组")
    await client.post("/api/reset", params={"formation": "crh16"})


async def _main_async() -> int:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await openapi_and_routes(client)
        await run_all(client)
    print("-" * 72)
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部断言通过（FastAPI 真实运行）。")
    return 0


def test_fastapi_end_to_end() -> None:
    """同步入口：让 pytest 与离线垫片都能收集到这一整组检查。

    pytest 默认不会执行 ``async def`` 用例（需要 anyio/asyncio 插件），
    因此这里用 ``asyncio.run`` 自己驱动事件循环——两种运行方式都能跑满。
    """
    asyncio.run(_run_checks())


async def _run_checks() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await openapi_and_routes(client)
        await run_all(client)


def main() -> int:
    """脚本模式入口（``python tests/test_fastapi_app.py``）。"""
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
