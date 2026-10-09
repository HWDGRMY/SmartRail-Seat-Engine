"""12306 风格购票流程的 HTTP 路由（用户模式 + 开发者模式）。

为什么单独放一个模块而不是塞进 ``stdlib_server``
------------------------------------------------
路由有 15 条，混在既有路由里会让那个文件难以阅读；而且这些路由**只依赖
:mod:`smartrail.ticketing` 的内存态**，与 ``STATE.engine`` 无关
（购票流程自己按台账建引擎，保证"用户看到的余票"与"求解器可用座位"同源）。

返回 ``(path, handler)`` 列表供服务器注册。``handler`` 统一签名
``(payload) -> (status, body)``，与既有路由一致。
"""

from __future__ import annotations

from typing import Any, Callable

from .. import ticketing
from ..ticketing import booking as booking_mod
from ..ticketing import devstore as devstore_mod
from ..ticketing import passenger_store as store_mod

Handler = Callable[[dict[str, Any]], "tuple[int, Any]"]

#: 页面路由：路径 → 静态文件名
TICKETING_PAGES: dict[str, str] = {
    "/ticket": "ticketing.html",
    "/dev": "developer.html",
}


# ---------------------------------------------------------------------------
# 用户模式
# ---------------------------------------------------------------------------


def passenger_types(payload: dict[str, Any]) -> tuple[int, Any]:
    """人群类型目录（"选择人群类型"下拉用）。"""
    return 200, store_mod.passenger_type_catalog()


def list_passengers(payload: dict[str, Any]) -> tuple[int, Any]:
    """乘车人列表。

    ``scope`` 区分两种用途（**这是需求要求的隔离**）：

    * ``user``（默认）：只返回**用户自己添加的**乘车人。用户模式用它，
      首次进入必然为空 -> 触发"请先添加乘车人"的引导。
    * ``dev``：返回全部，含内置预制档案（各类人群各一位）。
      **预制数据是给开发者模式做默认购票人的，不该给旅客看到**。
    """
    store = ticketing.get_passenger_store()
    scope = str(payload.get("scope") or "user").lower()
    if scope == "dev":
        profiles = store.all()
    else:
        profiles = store.user_added()
    return 200, {
        "scope": scope,
        "passengers": [profile.to_dict() for profile in profiles],
        "count": len(profiles),
        "preset_count": len(store.presets()),
        # 界面要据此判断"是否引导用户添加乘车人"
        "empty": not profiles,
    }


def add_passenger(payload: dict[str, Any]) -> tuple[int, Any]:
    store = ticketing.get_passenger_store()
    try:
        profile = store.add(payload)
    except ValueError as error:
        return 422, {"detail": str(error)}
    return 201, profile.to_dict()


def update_passenger(payload: dict[str, Any]) -> tuple[int, Any]:
    store = ticketing.get_passenger_store()
    profile_id = str(payload.get("profile_id") or "")
    try:
        profile = store.update(profile_id, payload)
    except ValueError as error:
        return 404, {"detail": str(error)}
    return 200, profile.to_dict()


def remove_passenger(payload: dict[str, Any]) -> tuple[int, Any]:
    store = ticketing.get_passenger_store()
    profile_id = str(payload.get("profile_id") or "")
    if not store.remove(profile_id):
        return 404, {"detail": f"乘车人不存在：{profile_id}"}
    return 200, {"removed": profile_id, "count": len(store.all())}


def list_trains(payload: dict[str, Any]) -> tuple[int, Any]:
    """车次列表：**只有余票数字，没有座位分布图**（用户模式要求）。"""
    store = ticketing.get_dev_store()
    return 200, {
        "trains": ticketing.available_trains(store),
        "wheelchair_bays": store.wheelchair_bays_summary(),
    }


def seat_rows(payload: dict[str, Any]) -> tuple[int, Any]:
    """选座服务：只返回一排 A/B/C/D/F。"""
    store = ticketing.get_dev_store()
    class_code = str(payload.get("class_code") or "二等座")
    return 200, {
        "class_code": class_code,
        "columns": ticketing.seat_row_options(store, class_code),
        "row_columns": list(booking_mod.ROW_COLUMNS),
    }


def evaluate(payload: dict[str, Any]) -> tuple[int, Any]:
    """下单前判定：选座要求能否满足、是否需要自动分票。"""
    store = ticketing.get_dev_store()
    class_code = str(payload.get("class_code") or "二等座")
    profile_ids = [str(item) for item in (payload.get("profile_ids") or [])]
    profiles = ticketing.get_passenger_store().by_ids(profile_ids)
    preference = booking_mod.SeatPreference.from_dict(payload.get("preference"))
    errors = store_mod.validate_selection(profiles)
    verdict = booking_mod.evaluate_preference(
        store, class_code, max(1, len(profiles)), preference
    )
    wheelchair = booking_mod.evaluate_wheelchair_bays(store, profiles)
    return 200, {
        "errors": errors,
        "verdict": verdict.to_dict(),
        "wheelchair": wheelchair.to_dict(),
        "passenger_count": len(profiles),
        "class_code": class_code,
        "preference": preference.to_dict(),
    }


def book(payload: dict[str, Any]) -> tuple[int, Any]:
    """提交订单：出票 + 写入开发者台账（用户/开发者实时同步）。"""
    store = ticketing.get_dev_store()
    class_code = str(payload.get("class_code") or "二等座")
    profile_ids = [str(item) for item in (payload.get("profile_ids") or [])]
    profiles = ticketing.get_passenger_store().by_ids(profile_ids)
    preference = booking_mod.SeatPreference.from_dict(payload.get("preference"))
    order_id = str(payload.get("order_id") or f"E{len(store.orders) + 1:04d}")
    result = ticketing.book_ticket_order(
        store, profiles,
        class_code=class_code,
        preference=preference,
        order_id=order_id,
    )
    if not result["ok"] and result["errors"]:
        return 422, {
            "ok": False,
            "errors": result["errors"],
            "verdict": result["verdict"],
        }
    return 200, result


def list_orders(payload: dict[str, Any]) -> tuple[int, Any]:
    store = ticketing.get_dev_store()
    return 200, {"orders": list(store.orders), "count": len(store.orders)}


# ---------------------------------------------------------------------------
# 开发者模式
# ---------------------------------------------------------------------------


def dev_snapshot(payload: dict[str, Any]) -> tuple[int, Any]:
    """全局座位图：全列座位 + 静音/无障碍标记 + 已售与分票映射。"""
    store = ticketing.get_dev_store()
    return 200, store.snapshot()


def dev_passengers(payload: dict[str, Any]) -> tuple[int, Any]:
    """开发者模式的乘车人列表：**含内置预制档案**。

    用户模式走同一个接口的 ``scope=user``，只看得到自己添加的人。
    """
    store = ticketing.get_passenger_store()
    profiles = store.all()
    return 200, {
        "scope": "dev",
        "passengers": [profile.to_dict() for profile in profiles],
        "count": len(profiles),
        "preset_count": len(store.presets()),
        "user_count": len(store.user_added()),
        "empty": not profiles,
    }


def dev_set_remaining(payload: dict[str, Any]) -> tuple[int, Any]:
    """调整某席别的模拟余票，**实时**影响用户模式。"""
    store = ticketing.get_dev_store()
    class_code = str(payload.get("class_code") or "二等座")
    target = payload.get("remaining")
    if target is None:
        return 422, {"detail": "缺少 remaining"}
    try:
        outcome = store.set_remaining(class_code, int(target))
    except ValueError as error:
        return 422, {"detail": str(error)}
    return 200, outcome


def dev_fill(payload: dict[str, Any]) -> tuple[int, Any]:
    """把整列车卖到指定上座率（压测用）。"""
    store = ticketing.get_dev_store()
    ratio = float(payload.get("ratio", 0.0))
    return 200, store.fill_to_ratio(ratio)


def dev_toggle_seat(payload: dict[str, Any]) -> tuple[int, Any]:
    """手动锁定/解锁单个座位（座位图上的点击操作）。

    刻意**不允许解锁"用户已售"的座位** —— 那不是开发者的锁，而是真实售出，
    放回去会让用户模式的余票凭空变多，属于数据不一致。
    """
    store = ticketing.get_dev_store()
    seat_id = str(payload.get("seat_id") or "")
    if not seat_id:
        return 422, {"detail": "缺少 seat_id"}
    if not any(seat.seat_id == seat_id for seat in store.formation.seats):
        return 404, {"detail": f"座位不存在：{seat_id}"}

    record = store.occupied.get(seat_id)
    if record is None:
        store.occupy([seat_id], source=devstore_mod.SOURCE_MANUAL)
        action = "locked"
    elif record.source == devstore_mod.SOURCE_SOLD:
        return 409, {"detail": f"{seat_id} 是用户已售座位，不能手动解锁"}
    else:
        store.release([seat_id])
        action = "unlocked"
    return 200, {
        "seat_id": seat_id,
        "action": action,
        "occupied": store.is_occupied(seat_id),
        "remaining": store.remaining(),
    }


def dev_reset(payload: dict[str, Any]) -> tuple[int, Any]:
    """重置系统：清空占用与订单，乘车人档案回到预制状态。"""
    ticketing.reset_dev_store()
    if payload.get("passengers", True):
        ticketing.reset_passenger_store()
    store = ticketing.get_dev_store()
    return 200, {
        "reset": True,
        "remaining": store.remaining(),
        "occupied_count": len(store.occupied),
        "order_count": len(store.orders),
        "passenger_count": len(ticketing.get_passenger_store().all()),
        # 用户模式只应看到用户添加的；重置后应为 0
        "user_passenger_count": len(ticketing.get_passenger_store().user_added()),
    }


#: GET 路由
GET_ROUTES: dict[str, Handler] = {
    "/api/passengers": list_passengers,
    "/api/passengers/types": passenger_types,
    "/api/trains": list_trains,
    "/api/trains/seat-row": seat_rows,
    "/api/tickets/orders": list_orders,
    "/api/dev/snapshot": dev_snapshot,
    "/api/dev/passengers": dev_passengers,
}

#: POST 路由
POST_ROUTES: dict[str, Handler] = {
    "/api/passengers/add": add_passenger,
    "/api/passengers/update": update_passenger,
    "/api/passengers/remove": remove_passenger,
    "/api/trains/evaluate": evaluate,
    "/api/tickets/book": book,
    "/api/dev/remaining": dev_set_remaining,
    "/api/dev/fill": dev_fill,
    "/api/dev/seat/toggle": dev_toggle_seat,
    "/api/dev/reset": dev_reset,
}


__all__ = [
    "GET_ROUTES",
    "POST_ROUTES",
    "TICKETING_PAGES",
    "add_passenger",
    "book",
    "dev_fill",
    "dev_passengers",
    "dev_reset",
    "dev_set_remaining",
    "dev_snapshot",
    "dev_toggle_seat",
    "evaluate",
    "list_orders",
    "list_passengers",
    "list_trains",
    "passenger_types",
    "remove_passenger",
    "seat_rows",
    "update_passenger",
]
