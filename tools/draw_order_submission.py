"""把"批量提交订单"的真实结果画成图（不依赖浏览器）。

用法::

    python tools/draw_order_submission.py              # 载入内置示例订单
    python tools/draw_order_submission.py --orders orders.json
    python tools/draw_order_submission.py --out 我的图.png

``--orders`` 指向一个 JSON 文件，内容与 ``/api/orders/submit`` 的 ``orders``
字段一致。缺省用内置示例：7 张订单，含 1 张必然无法满足的（8 位轮椅），
用来展示"未满足订单 + 可操作原因"的效果。

在受限环境中浏览器无法启动 crashpad/mojo 子进程，headless 截图不可用，
因此用 Pillow 按真实接口返回重绘。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "docs" / "screenshots" / "order-submission.png"
DEFAULT_API = "http://127.0.0.1:8000/api/orders/submit"
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

ORDER_COLORS = [
    (59, 142, 234), (46, 171, 91), (229, 80, 74), (230, 162, 60),
    (163, 113, 247), (22, 184, 191), (212, 100, 155), (122, 139, 63),
    (91, 127, 212), (194, 112, 58), (79, 158, 143), (139, 111, 214),
]
BG = (242, 244, 247)
CARD = (255, 255, 255)
INK = (51, 51, 51)
MUTED = (123, 135, 148)
LINE = (233, 237, 242)
BLUE = (59, 142, 234)
OK = (46, 171, 91)
WARN = (230, 162, 60)
BAD = (229, 80, 74)
LEVEL_COLOR = {
    "fulfilled": (46, 171, 91),
    "confirmed": (31, 139, 145),
    "action_required": (194, 112, 58),
    "partial": (230, 162, 60),
    "impossible": (229, 80, 74),
}

# 用户构造的订单：既有正常单，也有必然无法满足的单
ORDERS = [
    {"order_id": "ORD-1", "note": "带娃一家三口",
     "passengers": [{"key": "adult"}, {"key": "adult"}, {"key": "child"}]},
    {"order_id": "ORD-2", "note": "轮椅旅客 + 家属",
     "passengers": [{"key": "wheelchair"}, {"key": "caregiver"}, {"key": "adult"}]},
    {"order_id": "ORD-3", "note": "孕晚期 + 同伴",
     "passengers": [{"key": "pregnant"}, {"key": "caregiver"}]},
    {"order_id": "ORD-4", "note": "朋友团 5 人（软绑定）",
     "passengers": [{"key": "adult"}] * 5},
    {"order_id": "ORD-5", "note": "婴儿 + 看护人",
     "passengers": [{"key": "infant"}, {"key": "caregiver"}]},
    {"order_id": "ORD-6", "note": "视障（需导盲）+ 同伴",
     "passengers": [{"key": "blind_with_guide"}, {"key": "caregiver"}]},
    {"order_id": "ORD-7", "note": "8 位轮椅（专区不够，仍出票 + 询问）",
     "passengers": [{"key": "wheelchair"}] * 8},
]


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def load_orders(path: str | None) -> list[dict]:
    """读取订单清单；缺省用内置示例。"""
    if not path:
        return ORDERS
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return list(payload.get("orders") or [])
    return list(payload)


def submit(orders: list[dict], api: str, seed: int, fill: float) -> dict:
    request = urllib.request.Request(
        api,
        data=json.dumps({"orders": orders, "seed": seed, "fill": fill}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    result = submit(args.orders_list, args.api, args.seed, args.fill)
    summary = result["summary"]
    train = result["train"]
    orders = result["orders"]

    owner: dict[str, tuple[str, tuple[int, int, int], str]] = {}
    for order in orders:
        color = ORDER_COLORS[order["color_index"] % len(ORDER_COLORS)]
        for pid, seat_id in order["seats"].items():
            owner[seat_id] = (pid, color, order["note"] or order["order_id"])
    occupied = {s["seat_id"] for s in train["seats"] if s["occupied"]}

    W = 1600
    IMG_H = 2400
    img = Image.new("RGB", (W, IMG_H), BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, W, 88], fill=BLUE)
    draw.text((34, 14), "批量提交订单 · 真实数据", font=font(24, True), fill=(255, 255, 255))
    draw.text(
        (34, 52),
        f"用户自己组单：{summary['orders']} 张订单 / {summary['requested_passengers']} 人，"
        f"每单人数与乘客类型各不相同 · 空车起售",
        font=font(13), fill=(226, 238, 252),
    )

    y = 108
    draw.rounded_rectangle([24, y, W - 24, y + 118], 10, fill=CARD)
    metrics = [
        ("订单 / 乘客", f"{summary['orders']} / {summary['requested_passengers']}", INK),
        ("出票 / 候补", f"{summary['seated_passengers']} / {summary['waitlisted_passengers']}", INK),
        ("全部出票", str(summary["fulfilled_orders"]), OK),
        ("已确认例外后出票", str(summary.get("confirmed_orders", 0)), (31, 139, 145)),
        ("已出票需现场处理", str(summary["action_orders"]),
         (194, 112, 58) if summary["action_orders"] else INK),
        ("部分出票其余候补", str(summary["partial_orders"]),
         WARN if summary["partial_orders"] else INK),
        ("无座可发", str(summary["impossible_orders"]),
         BAD if summary["impossible_orders"] else OK),
        ("Tier 0 违规", str(summary["tier0_violations"]),
         BAD if summary["tier0_violations"] else OK),
        ("总耗时", f"{summary['wall_ms']} ms", INK),
    ]
    box_w = (W - 48 - 32) / len(metrics)
    for index, (label, value, color) in enumerate(metrics):
        x = 40 + index * box_w
        draw.rounded_rectangle([x, y + 14, x + box_w - 12, y + 80], 8, fill=(247, 249, 251))
        draw.text((x + 12, y + 22), label, font=font(11.5), fill=MUTED)
        draw.text((x + 12, y + 44), value, font=font(19, True), fill=color)

    # ---- 需要确认的例外 / 需要现场处理的订单 ----
    y += 118 + 16
    confirmations = result.get("confirmations") or []
    unmet = result["unmet_orders"]
    rows = len(confirmations) + len(unmet)
    panel_h = 40 + max(1, rows) * 62
    draw.rounded_rectangle([24, y, W - 24, y + panel_h], 10, fill=CARD)
    draw.text(
        (40, y + 13),
        f"需要确认的例外 {len(confirmations)} 张 · 需要现场处理 {len(unmet)} 张"
        " —— 票已出，例外已说明",
        font=font(15, True),
        fill=(194, 112, 58) if rows else OK,
    )
    uy = y + 42
    if not rows:
        draw.text((40, uy), "全部订单直接出票，无需确认或现场处理。",
                  font=font(13), fill=OK)
    for item in confirmations:
        draw.rectangle([40, uy, 44, uy + 50], fill=(194, 112, 58))
        draw.text((54, uy), f"？ {item['order_id']}", font=font(13, True), fill=INK)
        meta = (f"已出票 {item['seated']}/{item['requested']} 人"
                + (f" · 原因码 {item['code']}" if item.get("code") else ""))
        tw = draw.textlength(meta, font=font(11.5))
        draw.text((W - 40 - tw, uy + 1), meta, font=font(11.5), fill=MUTED)
        question = item.get("question") or ""
        draw.text((54, uy + 19), question[:64], font=font(11.5), fill=(138, 74, 28))
        if len(question) > 64:
            draw.text((54, uy + 34), question[64:128], font=font(11.5), fill=(138, 74, 28))
        uy += 62
    for item in unmet:
        color = LEVEL_COLOR.get(item["level"], WARN)
        draw.rectangle([40, uy, 44, uy + 50], fill=color)
        draw.text((54, uy), item["order_id"], font=font(13.5, True), fill=INK)
        tw = draw.textlength(item["order_id"], font=font(13.5, True))
        draw.text((54 + tw + 10, uy + 1), item["level_label"], font=font(12.5), fill=color)
        reason = item["reasons"][0] if item["reasons"] else ""
        draw.text((54, uy + 19), reason[:60], font=font(11.5), fill=(85, 95, 110))
        if len(reason) > 60:
            draw.text((54, uy + 34), reason[60:120], font=font(11.5), fill=(85, 95, 110))
        uy += 62

    # ---- 座位图 + 逐单结果 ----
    y += panel_h + 16
    left_w = int((W - 48) * 0.52)
    right_x = 24 + left_w + 16
    right_w = W - 24 - right_x
    cars_with_pax = {s["carriage"] for s in train["seats"] if s["seat_id"] in owner}
    cars = [c for c in train["carriages"] if c["number"] in cars_with_pax or c["accessible"]][:3]
    row_h = 26
    car_heights = [
        32 + len({s["row"] for s in train["seats"] if s["carriage"] == c["number"]}) * row_h + 10
        for c in cars
    ]
    left_h = 42 + sum(car_heights) + 10
    right_h = 42 + len(orders) * 44 + 10
    panel_h = max(left_h, right_h)
    draw.rounded_rectangle([24, y, W - 24, y + panel_h], 10, fill=CARD)
    draw.text((40, y + 13), "座位图（不同颜色 = 不同订单）", font=font(15, True), fill=INK)
    cy = y + 42
    for car, cheight in zip(cars, car_heights):
        draw.rounded_rectangle([40, cy, 40 + left_w - 32, cy + cheight], 7,
                               fill=(251, 252, 253),
                               outline=LINE if not car["quiet"] else (183, 169, 245))
        head = f"{car['number']:02d} 车"
        draw.text((52, cy + 7), head, font=font(12.5, True), fill=INK)
        hx = 52 + draw.textlength(head, font=font(12.5, True)) + 10
        if car["quiet"]:
            draw.text((hx, cy + 8), "静音", font=font(11), fill=(91, 70, 201))
            hx += 40
        if car["accessible"]:
            draw.text((hx, cy + 8), "无障碍专区", font=font(11), fill=(31, 139, 145))
        rows = sorted({s["row"] for s in train["seats"] if s["carriage"] == car["number"]})
        for index, row in enumerate(rows):
            ry = cy + 30 + index * row_h
            draw.text((52, ry + 5), str(row), font=font(10), fill=(168, 176, 186))
            cx = 74
            for seat in sorted(
                [s for s in train["seats"] if s["carriage"] == car["number"] and s["row"] == row],
                key=lambda s: s["col"],
            ):
                if cx + 25 > 40 + left_w - 32:
                    break
                hit = owner.get(seat["seat_id"])
                box = [cx, ry + 1, cx + 25, ry + 22]
                if hit:
                    pid, color, _l = hit
                    draw.rounded_rectangle(box, 4, fill=color)
                    label = pid.split("-")[0][:3]
                    tw = draw.textlength(label, font=font(9.5, True))
                    draw.text((cx + (25 - tw) / 2, ry + 5), label,
                              font=font(9.5, True), fill=(255, 255, 255))
                elif seat["seat_id"] in occupied:
                    draw.rounded_rectangle(box, 4, fill=(233, 237, 242))
                else:
                    fill = (255, 255, 255)
                    outline = (207, 214, 222)
                    if seat.get("quiet"):
                        fill, outline = (247, 245, 255), (183, 169, 245)
                    elif seat.get("accessible"):
                        fill, outline = (240, 251, 252), (127, 211, 216)
                    draw.rounded_rectangle(box, 4, fill=fill, outline=outline)
                    tw = draw.textlength(seat["col"], font=font(9.5))
                    draw.text((cx + (25 - tw) / 2, ry + 5), seat["col"],
                              font=font(9.5), fill=(125, 135, 148))
                cx += 28
        cy += cheight

    draw.text((right_x + 16, y + 13), "逐单结果（自己构造的订单）",
              font=font(15, True), fill=INK)
    oy = y + 42
    for order in orders:
        color = ORDER_COLORS[order["color_index"] % len(ORDER_COLORS)]
        lv = LEVEL_COLOR.get(order["level"], MUTED)
        draw.rectangle([right_x + 16, oy, right_x + 20, oy + 36], fill=color)
        draw.text((right_x + 28, oy), order["order_id"], font=font(12.5, True), fill=INK)
        note = order["note"] or ""
        nw = draw.textlength(order["order_id"], font=font(12.5, True))
        draw.text((right_x + 36 + nw, oy + 1), note[:20], font=font(11.5), fill=MUTED)
        right_text = f"{order['seated']}/{order['requested']} 出票 · {order['level_label']}"
        tw = draw.textlength(right_text, font=font(11.5))
        draw.text((right_x + right_w - 28 - tw, oy + 1), right_text, font=font(11.5), fill=lv)
        who = "＋".join(p["label"] for p in order["passengers_detail"])
        draw.text((right_x + 28, oy + 18), who[:40], font=font(11), fill=(85, 95, 110))
        oy += 44

    total_h = y + panel_h + 24
    img = img.crop((0, 0, W, min(total_h, IMG_H)))
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB  {img.size}")
    print(
        f"订单 {summary['orders']}，出票 {summary['seated_passengers']}/"
        f"{summary['requested_passengers']}，无法满足 {summary['impossible_orders']}，"
        f"耗时 {summary['wall_ms']} ms"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制批量提交订单结果图")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help=f"输出 PNG 路径（默认 {DEFAULT_OUT.relative_to(ROOT)}）")
    parser.add_argument("--api", default=DEFAULT_API, help="订单提交接口地址")
    parser.add_argument("--orders", help="订单清单 JSON 文件（缺省用内置示例）")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--fill", type=float, default=0.0)
    parsed = parser.parse_args()
    parsed.orders_list = load_orders(parsed.orders)
    return parsed


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
