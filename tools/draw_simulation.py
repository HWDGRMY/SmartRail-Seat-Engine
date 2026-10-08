"""把"按类型批量生成订单"的真实结果画成图（不依赖浏览器）。

用法::

    python tools/draw_simulation.py                      # 空车 + 全类型各 1 位
    python tools/draw_simulation.py --fill 0.6           # 六成上座率起售
    python tools/draw_simulation.py --counts adult=6 child=2 wheelchair=1

部分受限环境不允许浏览器启动 crashpad/mojo 子进程，headless 截图不可用，
因此用 Pillow 按**真实接口返回**重绘同样的布局，保证"能看见"。
所有数字与座位都来自真实结果，不做任何美化。
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

from smartrail.v3.archetypes import archetype_catalog  # noqa: E402

DEFAULT_OUT = ROOT / "docs" / "screenshots" / "concurrent-simulation.png"
DEFAULT_API = "http://127.0.0.1:8000/api/simulate/concurrent"

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


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def parse_counts(raw: list[str] | None) -> dict[str, int]:
    """解析 ``--counts adult=6 child=2`` 形式的参数。"""
    if not raw:
        return {item["key"]: 1 for item in archetype_catalog()}
    counts: dict[str, int] = {}
    valid = {item["key"] for item in archetype_catalog()}
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"--counts 需要 key=数量 形式，收到 {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        if key not in valid:
            raise SystemExit(f"未知乘客类型 {key!r}，可选：{', '.join(sorted(valid))}")
        counts[key] = int(value)
    return counts


def fetch(counts: dict[str, int], fill: float, seed: int, api: str) -> dict:
    """调用并发模拟接口，取真实结果。"""
    payload = {"archetypes": counts, "seed": seed, "fill": fill}
    request = urllib.request.Request(
        api,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    result = fetch(args.counts_map, args.fill, args.seed, args.api)
    summary = result["summary"]
    coverage = result["coverage"]
    train = result["train"]
    orders = result["orders"]

    owner: dict[str, tuple[str, tuple[int, int, int], str]] = {}
    for order in orders:
        color = ORDER_COLORS[order["color_index"] % len(ORDER_COLORS)]
        for pid, seat_id in order["seats"].items():
            owner[seat_id] = (pid, color, order["preset_label"])
    occupied = {s["seat_id"] for s in train["seats"] if s["occupied"]}

    W = 1600
    IMG_H = 2700
    """画布高度。

    必须**足够高**：PIL 会**静默丢弃**越界绘制，不会报错。这是本项目踩过的一个
    隐蔽坑 —— 早期用占位高度 200 建图，之后所有 y>200 的内容（座位图、逐单结果）
    全部没画上，最后 crop 又把画布撑到 2562，结果是一整片黑色，
    而日志里既没有异常也没有警告。
    """
    img = Image.new("RGB", (W, IMG_H), BG)
    draw = ImageDraw.Draw(img)
    card_pad = 20

    def card(y: int, height: int) -> int:
        draw.rounded_rectangle([24, y, W - 24, y + height], 10, fill=CARD)
        return y + 16

    # ---------------- 顶部标题 ----------------
    draw.rectangle([0, 0, W, 88], fill=BLUE)
    draw.text((34, 16), "并发下单模拟 · 真实数据", font=font(24, True), fill=(255, 255, 255))
    draw.text(
        (34, 52),
        f"模块化输入：{len(archetype_catalog())} 种乘客类型各 1 位 → 自动组单 "
        f"{summary['orders']} 张 / {summary['requested_passengers']} 人 · 空车起售",
        font=font(13), fill=(226, 238, 252),
    )

    y = 108
    # ---------------- 指标卡 ----------------
    top = card(y, 128)
    metrics = [
        ("订单 / 乘客", f"{summary['orders']} / {summary['requested_passengers']}", INK),
        ("出票 / 候补", f"{summary['seated_passengers']} / {summary['waitlisted_passengers']}", INK),
        ("就座率", f"{summary['seat_rate'] * 100:.1f}%", OK if summary["seat_rate"] >= 0.999 else WARN),
        ("Tier 0 违规", str(summary["tier0_violations"]), BAD if summary["tier0_violations"] else OK),
        ("同订单同车厢", f"{summary['orders_same_carriage']} / {summary['multi_passenger_orders']}", OK),
        ("未满足重点需求", str(summary["unmet_need_count"]), BAD if summary["unmet_need_count"] else OK),
        ("时延 P50 / P95", f"{summary['latency_ms']['p50']} / {summary['latency_ms']['p95']} ms", INK),
        ("人群覆盖", "缺 " + str(len(coverage["missing_support_needs"])) + " 项"
         if coverage["missing_support_needs"] else "全覆盖", WARN if coverage["missing_support_needs"] else OK),
    ]
    box_w = (W - 48 - 32) / 8
    for index, (label, value, color) in enumerate(metrics):
        x = 40 + index * box_w
        draw.rounded_rectangle([x, top - 4, x + box_w - 12, top + 62], 8, fill=(247, 249, 251))
        draw.text((x + 12, top + 4), label, font=font(12), fill=MUTED)
        draw.text((x + 12, top + 24), value, font=font(19, True), fill=color)
    draw.text(
        (40, top + 72),
        "覆盖支持需求：" + "、".join(coverage["support_needs"]),
        font=font(12.5 if False else 13), fill=MUTED,
    )
    y = top + 128 - 108 + 108  # 保持节奏，下面用绝对定位

    # ---------------- 座位图 + 逐单结果 ----------------
    y = 108 + 128 + 18
    left_w = int((W - 48) * 0.56)
    right_x = 24 + left_w + 16
    right_w = W - 24 - right_x

    # 只画"有本单乘客"的车厢，外加无障碍车厢；最多 4 节，避免图片过长
    cars_with_pax = {seat["carriage"] for seat in train["seats"] if seat["seat_id"] in owner}
    cars: list[dict] = []
    for car in train["carriages"]:
        if car["number"] in cars_with_pax or car["accessible"]:
            cars.append(car)
        if len(cars) >= 4:
            break
    row_h = 30
    car_heights = [
        34 + len({s["row"] for s in train["seats"] if s["carriage"] == car["number"]}) * row_h + 12
        for car in cars
    ]
    left_h = 44 + sum(car_heights) + 12
    right_h = 44 + len(orders) * 46 + 12
    panel_h = max(left_h, right_h)
    draw.rounded_rectangle([24, y, W - 24, y + panel_h], 10, fill=CARD)

    draw.text((40, y + 14), "座位图（不同颜色 = 不同订单）", font=font(15, True), fill=INK)
    cy = y + 44
    for car, cheight in zip(cars, car_heights):
        draw.rounded_rectangle(
            [40, cy, 40 + left_w - 32, cy + cheight], 7, fill=(251, 252, 253),
            outline=LINE if not car["quiet"] else (183, 169, 245),
        )
        head = f"{car['number']:02d} 车"
        draw.text((52, cy + 8), head, font=font(13, True), fill=INK)
        hx = 52 + draw.textlength(head, font=font(13, True)) + 10
        if car["quiet"]:
            draw.text((hx, cy + 9), "静音", font=font(11), fill=(91, 70, 201))
            hx += 40
        if car["accessible"]:
            draw.text((hx, cy + 9), "无障碍专区", font=font(11), fill=(31, 139, 145))
        rows = sorted({s["row"] for s in train["seats"] if s["carriage"] == car["number"]})
        for index, row in enumerate(rows):
            ry = cy + 32 + index * row_h
            draw.text((52, ry + 6), str(row), font=font(11), fill=(168, 176, 186))
            cx = 76
            row_seats = sorted(
                [s for s in train["seats"] if s["carriage"] == car["number"] and s["row"] == row],
                key=lambda s: s["col"],
            )
            for seat in row_seats:
                if cx + 27 > 40 + left_w - 32:
                    break
                hit = owner.get(seat["seat_id"])
                box = [cx, ry + 2, cx + 27, ry + 25]
                if hit:
                    pid, color, _label = hit
                    draw.rounded_rectangle(box, 4, fill=color)
                    label = pid.split("-")[0][:3]
                    tw = draw.textlength(label, font=font(10, True))
                    draw.text((cx + (27 - tw) / 2, ry + 7), label,
                              font=font(10, True), fill=(255, 255, 255))
                elif seat["seat_id"] in occupied:
                    draw.rounded_rectangle(box, 4, fill=(233, 237, 242))
                    tw = draw.textlength(seat["col"], font=font(10))
                    draw.text((cx + (27 - tw) / 2, ry + 7), seat["col"],
                              font=font(10), fill=(185, 193, 203))
                else:
                    fill = (255, 255, 255)
                    outline = (207, 214, 222)
                    if seat.get("quiet"):
                        fill, outline = (247, 245, 255), (183, 169, 245)
                    elif seat.get("accessible"):
                        fill, outline = (240, 251, 252), (127, 211, 216)
                    draw.rounded_rectangle(box, 4, fill=fill, outline=outline)
                    tw = draw.textlength(seat["col"], font=font(10))
                    draw.text((cx + (27 - tw) / 2, ry + 7), seat["col"],
                              font=font(10), fill=(125, 135, 148))
                cx += 30
        cy += cheight

    draw.text((right_x + 16, y + 14), "逐单结果（自动组单）", font=font(15, True), fill=INK)
    oy = y + 44
    for order in orders:
        color = ORDER_COLORS[order["color_index"] % len(ORDER_COLORS)]
        draw.rectangle([right_x + 16, oy, right_x + 20, oy + 38], fill=color)
        # 标题用"这张单里都有谁"，而不是内部类型键
        who = "＋".join(p["label"] for p in order.get("passengers", [])) or order["preset_label"]
        draw.text((right_x + 28, oy), who[:22], font=font(12.5, True), fill=INK)
        right_text = f"{order['seated']}/{order['requested']} 人 · 车厢 {','.join(map(str, order['carriages'])) or '—'}"
        tw = draw.textlength(right_text, font=font(11.5))
        draw.text((right_x + right_w - 28 - tw, oy + 2), right_text, font=font(11.5), fill=MUTED)
        seats = "　".join(f"{pid.split('-')[0]}@{sid}" for pid, sid in order["seats"].items())
        draw.text((right_x + 28, oy + 19), seats[:48] or "（未出票）", font=font(11), fill=(85, 95, 110))
        if order["unmet_needs"]:
            draw.text(
                (right_x + 28, oy + 31),
                ("⚠ " + "；".join(order["unmet_needs"]))[:52],
                font=font(10.5), fill=BAD,
            )
        oy += 46

    total_h = y + panel_h + 28
    img = img.crop((0, 0, W, total_h))
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB  {img.size}")
    summary = result["summary"]
    print(
        f"订单 {summary['orders']} / 乘客 {summary['requested_passengers']}，"
        f"出票 {summary['seated_passengers']}，Tier0 {summary['tier0_violations']}"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制批量并发下单结果图")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help=f"输出 PNG 路径（默认 {DEFAULT_OUT.relative_to(ROOT)}）")
    parser.add_argument("--api", default=DEFAULT_API, help="并发模拟接口地址")
    parser.add_argument("--fill", type=float, default=0.0, help="初始上座率 0~0.95")
    parser.add_argument("--seed", type=int, default=7, help="随机种子（便于复现）")
    parser.add_argument(
        "--counts", nargs="+", metavar="key=N",
        help="每种乘客的人数，如 adult=6 child=2 wheelchair=1；缺省为全类型各 1 位",
    )
    parsed = parser.parse_args()
    parsed.counts_map = parse_counts(parsed.counts)
    return parsed


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
