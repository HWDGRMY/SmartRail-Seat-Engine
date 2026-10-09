"""画出 12306 风格购票流程的两个模式（用户模式 / 开发者模式）。

用法::

    python tools/draw_ticketing.py

数据取自真实接口（车次余票、选座判定、开发者座位图），
用 Pillow 重绘界面布局。受限环境无法无头截图，原理见 tools/README.md。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "docs" / "screenshots" / "ticketing-flow.png"
BASE = "http://127.0.0.1:8000"
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

BG = (242, 244, 247)
CARD = (255, 255, 255)
INK = (51, 51, 51)
MUTED = (123, 135, 148)
LINE = (233, 237, 242)
BLUE = (59, 142, 234)
DARK = (43, 58, 77)
OK = (46, 171, 91)
WARN = (230, 162, 60)
BAD = (229, 80, 74)
ORANGE = (232, 145, 42)
QUIET = (109, 91, 208)
SOLD = (159, 178, 198)
MANUAL = (217, 199, 168)
PRESET = (201, 212, 224)


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def call(path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        body = response.read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_html": body}


class Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.img = Image.new("RGB", (width, height), BG)
        self.draw = ImageDraw.Draw(self.img)
        self.w = width

    def panel(self, x, y, w, h, fill=CARD, radius=9):
        self.draw.rounded_rectangle([x, y, x + w, y + h], radius, fill=fill)

    def text(self, x, y, content, size=12, bold=False, fill=INK):
        self.draw.text((x, y), content, font=font(size, bold), fill=fill)

    def right_text(self, right, y, content, size=12, bold=False, fill=INK):
        width = self.draw.textlength(content, font=font(size, bold))
        self.draw.text((right - width, y), content, font=font(size, bold), fill=fill)

    def tag(self, x, y, content, fg=BLUE, bg=(238, 244, 253)):
        width = self.draw.textlength(content, font=font(11)) + 14
        self.draw.rounded_rectangle([x, y, x + width, y + 18], 4, fill=bg)
        self.draw.text((x + 7, y + 2), content, font=font(11), fill=fg)
        return width


def main() -> int:
    # 重置到可复现状态
    call("/api/dev/reset", {"passengers": True})
    trains = call("/api/trains")["trains"]
    seat_row = call("/api/trains/seat-row?class_code=" + urllib.parse.quote("二等座"))
    passengers = call("/api/passengers")["passengers"]

    # 下两张订单：一张单人（带静音偏好），一张 3 人
    single = call("/api/tickets/book", {
        "class_code": "二等座", "profile_ids": ["C001"],
        "preference": {"columns": ["A"], "quiet": True},
    })
    family = call("/api/tickets/book", {
        "class_code": "二等座", "profile_ids": ["C002", "C005", "C006"],
        "preference": {"columns": [], "quiet": False},
    })
    verdict = call("/api/trains/evaluate", {
        "class_code": "二等座", "profile_ids": ["C002", "C005", "C006"],
        "preference": {"columns": [], "quiet": False},
    })
    snapshot = call("/api/dev/snapshot")

    W = 1600
    canvas = Canvas(W, 3000)
    draw = canvas.draw
    y = 0

    # ================= 标题 =================
    draw.rectangle([0, y, W, y + 84], fill=BLUE)
    canvas.text(34, y + 14, "12306 风格购票流程 · 真实数据", 23, True, (255, 255, 255))
    canvas.text(34, y + 50,
                "用户模式只显示一排选座偏好与余票数字，不展示整列座位图；"
                "开发者模式可看全列座位并实时改余票", 13, False, (226, 238, 252))
    y += 84 + 16

    # ================= 用户模式：车次列表 =================
    canvas.panel(24, y, W - 48, 250)
    canvas.text(40, y + 12, "用户模式 · 车次列表", 15, True, INK)
    # 副标题位置按主标题的**实际渲染宽度**算，而不是拍脑袋写偏移 ——
    # 写死 130 时与标题重叠了。
    title_width = draw.textlength("用户模式 · 车次列表", font=font(15, True))
    canvas.text(40 + title_width + 14, y + 16,
                "北京南 → 上海虹桥 · 10月19日 周一", 11.5, False, MUTED)
    ty = y + 42
    for index, train in enumerate(trains[:2]):
        row_h = 96
        draw.rounded_rectangle([40, ty, W - 40, ty + row_h], 8,
                               fill=(251, 252, 253), outline=LINE)
        canvas.text(56, ty + 10, train["train_code"], 20, True, (31, 45, 61))
        canvas.text(150, ty + 14, train["depart"], 17, True, INK)
        canvas.text(220, ty + 20, train["from"], 11.5, False, MUTED)
        canvas.text(300, ty + 20, f"── {train['duration']} ──", 11.5, False, MUTED)
        canvas.text(420, ty + 14, train["arrive"], 17, True, INK)
        canvas.text(492, ty + 20, train["to"], 11.5, False, MUTED)
        canvas.tag(560, ty + 12, train["tag"])
        tag_x = 620
        for car in train["quiet_carriages"]:
            tag_x += canvas.tag(tag_x, ty + 12, f"{car:02d}车 静音", QUIET, (243, 240, 253)) + 6
        # 席别行
        cx = 56
        for item in train["classes"]:
            canvas.text(cx, ty + 46, item["class_code"].replace("座", ""), 13.5, True, INK)
            canvas.text(cx, ty + 66, f"¥{item['price']}", 14, True, ORANGE)
            color = {"有票": OK, "无": (184, 192, 201)}.get(item["status"], ORANGE)
            canvas.text(cx + 78, ty + 49, item["status"], 12.5, False, color)
            draw.rounded_rectangle([cx + 130, ty + 46, cx + 178, ty + 72], 5,
                                   fill=BLUE if item["bookable"] else (210, 218, 226))
            canvas.text(cx + 142, ty + 51, "预订", 12,
                        False, (255, 255, 255))
            cx += 220
        ty += row_h + 10
    y += 250 + 16

    # ================= 用户模式：确认订单 =================
    canvas.panel(24, y, W - 48, 320)
    canvas.text(40, y + 12, "用户模式 · 确认订单 / 选座服务", 15, True, INK)
    title_width = draw.textlength("用户模式 · 确认订单 / 选座服务", font=font(15, True))
    canvas.text(40 + title_width + 14, y + 16,
                "选座服务仅显示一排 A / B / C / D / F；静音车厢可勾选", 11.5, False, MUTED)
    # 席别卡片
    cy = y + 42
    cx = 40
    for item in trains[0]["classes"]:
        on = item["class_code"] == "二等座"
        draw.rounded_rectangle([cx, cy, cx + 200, cy + 62], 8,
                               fill=(247, 251, 255) if on else (255, 255, 255),
                               outline=BLUE if on else LINE, width=2 if on else 1)
        canvas.text(cx + 14, cy + 8, item["class_code"], 14, True, INK)
        canvas.text(cx + 14, cy + 30, f"¥{item['price']} {item['discount']}",
                    12, False, ORANGE)
        color = {"有票": OK, "无": (184, 192, 201)}.get(item["status"], ORANGE)
        canvas.text(cx + 120, cy + 30, item["status"], 12, False, color)
        if on:
            canvas.tag(cx + 120, cy + 8, "已选", BLUE)
        cx += 212
    cy += 78
    # 一排座位
    canvas.text(40, cy, "选座偏好", 12.5, True, INK)
    sx = 130
    for column in seat_row["columns"]:
        on = column["col"] == "A"
        if column["col"] == "D":
            canvas.text(sx + 4, cy - 2, "过", 11, False, MUTED)
            canvas.text(sx + 4, cy + 12, "道", 11, False, MUTED)
            sx += 34
        draw.rounded_rectangle([sx, cy - 4, sx + 50, cy + 44], 7,
                               fill=(234, 243, 253) if on else (255, 255, 255),
                               outline=BLUE if on else (207, 214, 222),
                               width=2 if on else 1)
        canvas.text(sx + 21, cy + 2, column["col"], 15, True, (70, 82, 95))
        canvas.text(sx + 16, cy + 24, str(column["remaining"]), 10.5, False, MUTED)
        sx += 56
    canvas.text(sx + 10, cy + 12,
                "← 点击选择靠窗/过道偏好；不展示整列座位分布", 11.5, False, MUTED)
    cy += 58
    # 静音勾选
    draw.rounded_rectangle([40, cy, 56, cy + 16], 3, fill=(255, 255, 255),
                           outline=(207, 214, 222))
    draw.line([43, cy + 8, 46, cy + 12], fill=OK, width=2)
    draw.line([46, cy + 12, 53, cy + 3], fill=OK, width=2)
    canvas.text(64, cy, "请优先为我分配「静音车厢」席位", 12.5, False, INK)
    canvas.text(310, cy + 2, f"（03、11 车；二等座余票 "
                             f"{sum(c['remaining'] for c in seat_row['columns'])}）",
                11, False, MUTED)
    cy += 30
    # 乘车人
    canvas.text(40, cy, "乘车人", 12.5, True, INK)
    canvas.text(96, cy + 2, "已选 1 人（顺序即选座优先顺序）", 11, False, MUTED)
    draw.rounded_rectangle([W - 200, cy - 4, W - 40, cy + 22], 5,
                           fill=(255, 255, 255), outline=LINE)
    canvas.text(W - 188, cy + 1, "+ 选择乘车人", 12, False, (85, 95, 110))
    cy += 32
    order = single["order"]
    seat = order["passengers"][0]
    draw.rounded_rectangle([40, cy, W - 40, cy + 46], 8, fill=(247, 251, 255),
                           outline=BLUE)
    canvas.text(56, cy + 6, seat["name"], 14, True, INK)
    canvas.tag(120, cy + 5, "成人")
    canvas.tag(176, cy + 5, "已选")
    canvas.text(56, cy + 26, f"{seat['seat_id']} · {seat['class_code']}"
                             f"{' · 静音车厢' if seat['quiet'] else ''}",
                11.5, False, MUTED)
    canvas.right_text(W - 56, cy + 14, "出票成功", 13, True, OK)
    cy += 56
    verdict_box = (verdict["verdict"], single["order"])
    canvas.text(40, cy, "判定：", 12, True, INK)
    canvas.text(82, cy, verdict_box[0].get("reason", ""), 12, False,
                WARN if verdict_box[0].get("split_needed") else OK)
    y += 320 + 16

    # ================= 开发者模式：座位图 =================
    car_h = 132
    shown_cars = [1, 2, 3, 4]
    panel_h = 60 + len(shown_cars) * car_h
    canvas.panel(24, y, W - 48, panel_h)
    canvas.text(40, y + 12, "开发者模式 · 全局座位图", 15, True, INK)
    title_width = draw.textlength("开发者模式 · 全局座位图", font=font(15, True))
    canvas.text(40 + title_width + 14, y + 16,
                f"{snapshot['total_seats']} 座 · 已售 {snapshot['occupied_count']}"
                f" · 静音车厢 03/11 · 无障碍 04/12", 11.5, False, MUTED)
    # 图例
    lx = W - 620
    for label, color in (("可选", (255, 255, 255)), ("用户已售", SOLD),
                         ("手动锁定", MANUAL), ("预设占用", PRESET)):
        draw.rounded_rectangle([lx, y + 16, lx + 13, y + 29], 3,
                               fill=color, outline=(207, 214, 222))
        canvas.text(lx + 18, y + 16, label, 11, False, MUTED)
        lx += 76
    cy = y + 44
    by_car = {}
    for seat in snapshot["seats"]:
        by_car.setdefault(seat["carriage"], []).append(seat)
    for number in shown_cars:
        car = next(c for c in snapshot["carriages"] if c["number"] == number)
        seats = by_car[number]
        rows: dict[int, dict[str, dict]] = {}
        for seat in seats:
            rows.setdefault(seat["row"], {})[seat["col"]] = seat
        draw.rounded_rectangle([40, cy, W - 40, cy + car_h - 8], 8,
                               fill=(251, 252, 253), outline=LINE)
        canvas.text(56, cy + 6, f"{number:02d}车", 14, True, INK)
        canvas.text(104, cy + 9,
                    f"{car['class_code']} · {car['rows']}排 · "
                    f"定员 {car['total']} · 余 {car['remaining']}",
                    11, False, MUTED)
        tx = 380
        if car["quiet"]:
            tx += canvas.tag(tx, cy + 6, "静音车厢", QUIET, (243, 240, 253)) + 6
        if car["accessible"]:
            tx += canvas.tag(tx, cy + 6, "带残疾人卫生间", (22, 163, 163),
                             (232, 248, 248)) + 6
        sy = cy + 30
        for row_no in sorted(rows)[:4]:
            canvas.text(56, sy + 2, str(row_no), 10, False, MUTED)
            sx = 78
            for col in car["columns"]:
                seat = rows[row_no].get(col)
                if seat is None:
                    sx += 28
                    continue
                if seat["source"] == "sold":
                    fill, fg = SOLD, (255, 255, 255)
                elif seat["source"] == "manual":
                    fill, fg = MANUAL, (107, 90, 61)
                elif seat["source"] == "preset":
                    fill, fg = PRESET, (90, 106, 124)
                else:
                    fill, fg = (255, 255, 255), (152, 162, 174)
                draw.rounded_rectangle([sx, sy, sx + 25, sy + 21], 3,
                                       fill=fill, outline=(207, 214, 222))
                if seat["quiet"]:
                    draw.rectangle([sx, sy, sx + 25, sy + 21], outline=QUIET, width=2)
                if seat["accessible"]:
                    draw.line([sx + 1, sy + 18, sx + 24, sy + 18],
                              fill=(22, 163, 163), width=2)
                canvas.text(sx + 9, sy + 4, col, 9.5, False, fg)
                sx += 28
            sy += 23
        more = len(rows) - 4
        if more > 0:
            canvas.text(500, cy + car_h - 30, f"…… 另有 {more} 排",
                        10.5, False, MUTED)
        cy += car_h
    y += panel_h + 16

    # ================= 开发者模式：余票滑杆 =================
    canvas.panel(24, y, W - 48, 150)
    canvas.text(40, y + 12, "开发者模式 · 模拟余票（实时影响用户模式）", 15, True, INK)
    sy = y + 44
    for name, left in snapshot["remaining"].items():
        total = sum(1 for s in snapshot["seats"] if s["class_code"] == name)
        canvas.text(40, sy, name, 12.5, True, INK)
        draw.rounded_rectangle([120, sy + 4, 700, sy + 12], 4, fill=(233, 237, 242))
        ratio = left / max(1, total)
        draw.rounded_rectangle([120, sy + 4, 120 + int(580 * ratio), sy + 12],
                               4, fill=BLUE)
        canvas.text(716, sy, str(left), 13, True, INK)
        canvas.text(760, sy + 1, f"/ {total}", 11, False, MUTED)
        canvas.text(840, sy + 1,
                    "已售罄" if left == 0 else ("充足" if left >= 20 else "紧张"),
                    11, False, BAD if left == 0 else (OK if left >= 20 else WARN))
        sy += 28
    y += 150 + 16

    # ================= 分票映射 =================
    canvas.panel(24, y, W - 48, 176)
    canvas.text(40, y + 12, "开发者模式 · 用户下单记录（分票映射）", 15, True, INK)
    oy = y + 44
    for record in [family["order"], single["order"]]:
        draw.rectangle([40, oy, 44, oy + 52], fill=OK)
        canvas.text(56, oy, record["order_id"], 13, True, INK)
        tx = 118
        tx += canvas.tag(tx, oy - 1, record["class_code"]) + 6
        if record["split"]:
            tx += canvas.tag(tx, oy - 1, "自动分票", WARN, (253, 248, 240)) + 6
        canvas.text(56, oy + 20,
                    "、".join(record["rows"]) + f"　共 {record['seated']} 张",
                    11.5, False, MUTED)
        detail = "　".join(
            f"{p['name']}→{p['seat_id']}{'(静音)' if p['quiet'] else ''}"
            for p in record["passengers"]
        )
        canvas.text(56, oy + 36, detail, 11, False, (110, 120, 132))
        oy += 60
    y += 176 + 16

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    img = canvas.img.crop((0, 0, W, min(y, 3000)))
    img.save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB  {img.size}")
    print(f"车次 {len(trains)}｜乘车人 {len(passengers)}｜"
          f"订单 {len(snapshot['orders'])}｜已售 {snapshot['occupied_count']}｜"
          f"单人座位 {seat['seat_id']}｜3人 {family['order']['rows']}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制 12306 风格购票流程示意图")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help=f"输出 PNG 路径（默认 {DEFAULT_OUT.relative_to(ROOT)}）")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
