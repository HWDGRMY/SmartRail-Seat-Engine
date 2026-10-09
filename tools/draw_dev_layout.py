"""画开发者页的**新布局**：座位图占满整宽、车厢横向并排。

用法::

    python tools/draw_dev_layout.py

背景：座位图原先被塞在 1080px 的左列里，而一节车厢的座位网格只有
159px 宽 —— 每节右边留 1300px 空白，16 节竖排还要滚很久，像草稿纸。
现在座位图占满整宽、车厢按 minmax(250px,1fr) 自动并排。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "docs" / "screenshots" / "dev-layout.png"
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
SOLD = (159, 178, 198)
PRESET = (201, 212, 224)
MANUAL = (217, 199, 168)
QUIET = (109, 91, 208)
ACCESS = (22, 163, 163)
BAD = (229, 80, 74)

# 与 developer.html 的 CSS 对齐（改样式时这里要同步）
CELL_W, CELL_H, GRID_GAP, RNO_W, CARD_PAD = 25, 21, 2, 26, 12
MIN_COL, GAP, SHOW_ROWS = 250, 11, 6
W = 1560
WRAP_PAD = 22


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def call(path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read().decode("utf-8"))
        except json.JSONDecodeError:
            return {}


class Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.img = Image.new("RGB", (width, height), BG)
        self.draw = ImageDraw.Draw(self.img)

    def text(self, x, y, content, size=12, bold=False, fill=INK):
        self.draw.text((x, y), content, font=font(size, bold), fill=fill)

    def right(self, right, y, content, size=12, bold=False, fill=INK):
        width = self.draw.textlength(content, font=font(size, bold))
        self.draw.text((right - width, y), content, font=font(size, bold), fill=fill)

    def card(self, x, y, w, h, fill=CARD, outline=None):
        self.draw.rounded_rectangle([x, y, x + w, y + h], 9, fill=fill,
                                    outline=outline)

    def button(self, x, y, label, w=26, h=22, fill=(251, 252, 253), fg=INK,
               bold=False):
        self.draw.rounded_rectangle([x, y, x + w, y + h], 5, fill=fill,
                                    outline=LINE)
        size = font(12, bold)
        tw = self.draw.textlength(label, font=size)
        self.draw.text((x + (w - tw) / 2, y + (h - 15) / 2), label,
                       font=size, fill=fg)


def seat_style(seat: dict) -> tuple[tuple[int, int, int], tuple[int, int, int], int]:
    if not seat["occupied"]:
        fill, edge, width = (255, 255, 255), (207, 214, 222), 1
    elif seat["source"] == "sold":
        fill, edge, width = SOLD, (142, 163, 186), 1
    elif seat["source"] == "manual":
        fill, edge, width = MANUAL, (200, 179, 148), 1
    else:
        fill, edge, width = PRESET, (183, 196, 211), 1
    if seat["quiet"]:
        edge, width = QUIET, 2
    return fill, edge, width


def carriage_height(car: dict) -> int:
    rows = min(car["rows"], SHOW_ROWS)
    extra = 14 if car["rows"] > SHOW_ROWS else 0
    return 30 + rows * (CELL_H + GRID_GAP) + CARD_PAD * 2 + extra


def draw_carriage(canvas, x, y, w, car, seats) -> None:
    draw = canvas.draw
    # 车厢头只有一行：编号 + 车型/排数 + 标签 + 右侧定员。
    # 早先把标签放第二行，结果标签条压在座位网格上（坐标没对齐）；
    # 改成单行 + 按实测宽度决定谁让位，就没有这个问题。
    head_h = 30
    draw.rounded_rectangle([x, y, x + w, y + head_h], 8, fill=(251, 252, 253),
                           outline=LINE)
    canvas.text(x + 12, y + 4, f"{car['number']:02d}车", 13.5, True, INK)
    right_text = f"定员 {car['total']} ／ 余 {car['remaining']}"
    right_w = draw.textlength(right_text, font=font(11))
    canvas.text(x + w - 12 - right_w, y + 5, right_text, 11, False, MUTED)

    tags = []
    if car.get("quiet"):
        tags.append(("静音", QUIET, (243, 240, 253), (226, 221, 250)))
    if car.get("wheelchair_bays"):
        tags.append((f"轮椅位{car['wheelchair_bays']}", (15, 125, 125),
                     (230, 247, 247), (169, 222, 222)))
    tag_w = sum(int(draw.textlength(t, font=font(10))) + 14 + 5 for t, *_ in tags)

    spec_left = x + 62
    spec_right = x + w - 12 - right_w - 8 - tag_w
    spec = (f"{car.get('class_summary') or car['class_code']} · "
            f"{car['rows']} 排 · {''.join(car['columns'])}")
    available = spec_right - spec_left
    if available > 24:
        while draw.textlength(spec, font=font(10.5)) > available and len(spec) > 6:
            spec = spec[:-2]
        canvas.text(spec_left, y + 5, spec, 10.5, False, MUTED)
    tx = spec_right + 8
    for label, fg, bg, edge in tags:
        tw = int(draw.textlength(label, font=font(10))) + 14
        draw.rounded_rectangle([tx, y + 8, tx + tw, y + 24], 4, fill=bg,
                               outline=edge)
        canvas.text(tx + 7, y + 9, label, 10, False, fg)
        tx += tw + 5

    height = carriage_height(car)
    draw.rounded_rectangle([x, y + head_h, x + w, y + height], 8, fill=CARD,
                           outline=LINE)
    rows: dict[int, dict[str, dict]] = defaultdict(dict)
    for seat in seats:
        rows[seat["row"]][seat["col"]] = seat
    # 座位网格从**车厢头总高**之下开始（含标签行），
    # 否则标签行会压在座位图上。
    gy = y + head_h + CARD_PAD
    for row_no in sorted(rows)[:SHOW_ROWS]:
        canvas.text(x + CARD_PAD, gy + 4, str(row_no), 10.5, False, MUTED)
        cx = x + CARD_PAD + RNO_W
        for col in car["columns"]:
            seat = rows[row_no].get(col)
            if seat is not None:
                fill, edge, width = seat_style(seat)
                draw.rounded_rectangle([cx, gy, cx + CELL_W, gy + CELL_H], 3,
                                       fill=fill, outline=edge, width=width)
                tw = draw.textlength(col, font=font(9))
                draw.text((cx + (CELL_W - tw) / 2, gy + 4), col, font=font(9),
                          fill=(255, 255, 255) if seat["occupied"]
                          else (152, 162, 174))
                if seat.get("bay_slot"):
                    draw.rectangle([cx, gy + CELL_H - 3, cx + CELL_W, gy + CELL_H],
                                   fill=ACCESS)
            cx += CELL_W + GRID_GAP
        gy += CELL_H + GRID_GAP
    if car["rows"] > SHOW_ROWS:
        canvas.text(x + CARD_PAD, gy + 1,
                    f"… 还有 {car['rows'] - SHOW_ROWS} 排", 10, False, MUTED)


def blank_composition(schema: dict, adult: int, child: int = 0,
                      note: str = "") -> dict:
    bands = [b["id"] for b in schema["age_bands"]]
    return {
        "note": note,
        "base": {"adult": adult, "child": child, "youth": 0,
                 "toddler": 0, "infant": 0},
        "child_sub": {g["id"]: 0 for g in schema["child_sub_groups"]},
        "disability": {lv["id"]: {b: 0 for b in bands}
                       for lv in schema["disability_levels"]},
        "pregnant": {st["id"]: {b: 0 for b in bands}
                     for st in schema["pregnant_stages"]},
    }


def main() -> int:
    call("/api/dev/reset", {"passengers": True})
    schema = call("/api/composition/schema")
    # 真实提交几张单（含一张会被拦下的），这样"下单记录"有内容可画
    for payload in (
        blank_composition(schema, 2, 2, "一家四口"),
        blank_composition(schema, 0, 1, "无成人陪儿童"),
        blank_composition(schema, 3, 0, "三成人"),
    ):
        call("/api/composition/submit", {"orders": [payload]})
    snapshot = call("/api/dev/snapshot")
    by_car: dict[int, list[dict]] = defaultdict(list)
    for seat in snapshot["seats"]:
        by_car[seat["carriage"]].append(seat)
    cars = snapshot["carriages"]

    canvas = Canvas(W, 2200)
    draw = canvas.draw
    draw.rectangle([0, 0, W, 74], fill=DARK)
    canvas.text(28, 10, "开发者页布局：座位图占满整宽、车厢自动并排", 21, True,
                (255, 255, 255))
    canvas.text(28, 44, "原先座位图被塞在 1080px 左列里，而每节车厢座位只有 "
                        "159px 宽 —— 每节右边留 1300px 空白", 12.5, False,
                (190, 205, 220))

    y = 92
    # ---------- 概览 ----------
    canvas.card(22, y, W - 44, 58)
    canvas.text(42, y + 8, "概览", 13, True, INK)
    mx = 420
    for label, value in (
        ("定员", str(snapshot["total_seats"])),
        ("已售", str(snapshot["occupied_count"])),
        ("余票", str(sum(snapshot["remaining"].values()))),
        ("轮椅停放位", f"{snapshot['wheelchair_bays']['free']}"
                       f"/{snapshot['wheelchair_bays']['total']}"),
    ):
        canvas.text(mx, y + 8, label, 10.5, False, MUTED)
        canvas.text(mx, y + 24, value, 16, True, INK)
        mx += 145
    draw.rounded_rectangle([W - 148, y + 14, W - 42, y + 44], 6, fill=BAD)
    canvas.text(W - 130, y + 20, "重置系统", 12.5, True, (255, 255, 255))
    y += 58 + 14

    # ---------- 座位图（整宽） ----------
    content_w = W - 44 - 24
    cols = max(1, (content_w + GAP) // (MIN_COL + GAP))
    col_w = (content_w - GAP * (cols - 1)) // cols
    rows_of_cars = -(-len(cars) // cols)
    row_heights = []
    for r in range(rows_of_cars):
        chunk = cars[r * cols:(r + 1) * cols]
        row_heights.append(max(carriage_height(c) for c in chunk) + GAP)

    head = 12 + 18 + 22 + 20 + 30 + 14   # 标题/图例/停放位/间距
    card_h = head + sum(row_heights)
    canvas.card(22, y, W - 44, card_h)
    canvas.text(42, y + 12, "全局座位图", 15.5, True, INK)
    canvas.text(140, y + 14, "16 节编组 · 点击座位可手动锁定/解锁", 11.5, False,
                MUTED)
    canvas.right(W - 42, y + 14, "车厢按宽度自动并排", 11.5, False, MUTED)

    ly = y + 44
    lx = 42
    for label, color, filled in (
        ("可选", (255, 255, 255), True),
        ("用户已售", SOLD, True),
        ("手动锁定", MANUAL, True),
        ("预设占用", PRESET, True),
        ("静音车厢", QUIET, False),
        ("近轮椅位", ACCESS, True),
    ):
        draw.rounded_rectangle([lx, ly, lx + 16, ly + 12], 3,
                               fill=color if filled else CARD,
                               outline=color if not filled else (180, 190, 200),
                               width=2 if not filled else 1)
        canvas.text(lx + 22, ly - 2, label, 11.5, False, INK)
        lx += 22 + int(draw.textlength(label, font=font(11.5))) + 22

    ly += 22
    canvas.text(42, ly, "轮椅固定停放位 4 个（04 车、12 车各 2 个）", 11, False,
                MUTED)
    bx = 340
    for bay in snapshot["wheelchair_bays"]["bays"]:
        state = bay.get("passenger_name") or "空闲"
        draw.rounded_rectangle([bx, ly - 3, bx + 84, ly + 17], 4,
                               fill=(253, 242, 241) if bay["occupied"]
                               else (230, 247, 247),
                               outline=BAD if bay["occupied"] else ACCESS)
        canvas.text(bx + 8, ly - 1, bay["bay_id"], 11, True,
                    BAD if bay["occupied"] else (15, 125, 125))
        canvas.text(bx + 92, ly, state, 11, False, MUTED)
        bx += 92 + int(draw.textlength(state, font=font(11))) + 22

    top = y + head
    for index, car in enumerate(cars):
        r, c = divmod(index, cols)
        cx = 22 + 12 + c * (col_w + GAP)
        cy = top + sum(row_heights[:r])
        draw_carriage(canvas, cx, cy, col_w, car, by_car[car["number"]])
    y += card_h + 14

    # ---------- 操作面板：提交订单整行，其余按比例 ----------
    card_x = 22
    card_w = W - 44

    # ① 提交订单（整行）
    ry = y
    composer_h = 250
    canvas.card(card_x, ry, card_w, composer_h)
    canvas.text(card_x + 16, ry + 10, "提交订单", 14, True, INK)
    canvas.text(card_x + 86, ry + 12,
                "按基础分组加人数 → 需要时叠加特殊人群 → 提交", 11, False, MUTED)
    draw.rounded_rectangle([card_x + 16, ry + 34, card_x + 196, ry + 62], 6,
                           fill=(251, 252, 253), outline=LINE)
    canvas.text(card_x + 28, ry + 40, "二等座", 12.5, False, INK)
    canvas.right(card_x + card_w - 16, ry + 40, "共 6 人", 12, True, BLUE)
    # 基础分组（左）
    bx = card_x + 16
    by = ry + 78
    canvas.text(bx, by, "基础分组", 12.5, True, (59, 74, 90))
    canvas.text(bx + 66, by + 1, "总人数只由这 5 类决定", 10.5, False, MUTED)
    by += 20
    for group in schema["base_groups"]:
        value = {"adult": 4, "child": 2}.get(group["id"], 0)
        canvas.text(bx, by, group["label"], 12, True, INK)
        canvas.text(bx + 58, by + 2, group["desc"], 10, False, MUTED)
        ctl = bx + 250
        canvas.button(ctl, by - 2, "−", w=22, h=20,
                      fg=MUTED if value == 0 else INK)
        canvas.text(ctl + 28, by, str(value), 12, True, INK)
        canvas.button(ctl + 52, by - 2, "+", w=22, h=20)
        by += 28
    # 特殊人群（右）
    sx = card_x + 400
    sy = ry + 78
    canvas.text(sx, sy, "特殊人群", 12.5, True, (59, 74, 90))
    canvas.text(sx + 66, sy + 1, "叠加在基础分组之上，不改变总人数",
                10.5, False, MUTED)
    canvas.right(card_x + card_w - 16, sy, "展开 ▾", 11, False, MUTED)
    sy += 22
    for group in schema["child_sub_groups"]:
        value = 1 if group["id"] == "child_noisy" else 0
        canvas.text(sx, sy, group["label"], 11.5, False, INK)
        canvas.button(sx + 150, sy - 2, "−", w=20, h=19,
                      fg=MUTED if value == 0 else INK)
        canvas.text(sx + 174, sy, str(value), 11.5, True, INK)
        canvas.button(sx + 194, sy - 2, "+", w=20, h=19)
        sy += 26
    canvas.text(sx, sy + 2, "残疾旅客 / 孕妇：程度 × 年龄段（展开后设置）",
                10.5, False, MUTED)
    y += composer_h + GAP

    # ② 模拟余票 / 快速压测（各占一半；下单记录放在下面的整行明细里，
    #    避免"下单记录"出现两次）
    half = (card_w - GAP) // 2
    panels = [
        ("模拟余票", "调整后实时影响用户模式",
         [f"一等座 {snapshot['remaining'].get('一等座', 0)} / 64",
          f"商务座 {snapshot['remaining'].get('商务座', 0)} / 22",
          f"二等座 {snapshot['remaining'].get('二等座', 0)} / 1152"]),
        ("快速压测", "按真实售票打散占用",
         ["空车 / 三成 / 六成 / 八成五 / 满座",
          "碎片化：最长连座 2 · ≥2 连座 7 处"]),
    ]
    panel_h = 40 + max(len(p[2]) for p in panels) * 18 + 16
    for index, (title, hint, lines) in enumerate(panels):
        px = card_x + index * (half + GAP)
        canvas.card(px, y, half, panel_h)
        canvas.text(px + 16, y + 10, title, 13.5, True, INK)
        if hint:
            canvas.text(px + 16, y + 28, hint, 10.5, False, MUTED)
        for li, line in enumerate(lines):
            canvas.text(px + 16, y + 48 + li * 18, line[:44], 11.5, False, INK)
    y += panel_h + GAP

    # ③ 下单记录明细（整行）
    orders = snapshot["orders"]
    cards = max(1, (card_w - 24 + 10) // 350)
    cell_w = (card_w - 24 - 10 * (cards - 1)) // cards
    per_card = 74
    rows_needed = max(1, -(-len(orders) // cards)) if orders else 1
    record_h = 40 + rows_needed * per_card + 14
    canvas.card(card_x, y, card_w, record_h)
    canvas.text(card_x + 16, y + 10, "下单记录", 14, True, INK)
    canvas.text(card_x + 86, y + 12,
                f"共 {len(orders)} 单 · 含开发者组单与用户模式下单", 11, False,
                MUTED)
    if not orders:
        canvas.text(card_x + 16, y + 42, "还没有订单。", 11.5, False, MUTED)
    for index, order in enumerate(orders):
        r, c = divmod(index, cards)
        ox = card_x + 12 + c * (cell_w + 10)
        oy = y + 36 + r * per_card
        blocked = order.get("blocked")
        draw.rounded_rectangle([ox, oy, ox + cell_w, oy + per_card - 8], 8,
                               fill=(253, 247, 246) if blocked else (253, 254, 254),
                               outline=(240, 216, 213) if blocked else LINE)
        tx = ox + 10
        canvas.text(tx, oy + 7, order["order_id"], 12, True, INK)
        tx += int(draw.textlength(order["order_id"], font=font(12, True))) + 10
        for tag, color in ((order.get("level_label") or "", MUTED),
                           ("开发者组单" if order.get("source") == "dev-composition"
                            else "用户下单", (47, 122, 208))):
            if not tag:
                continue
            tw = int(draw.textlength(tag, font=font(10))) + 12
            draw.rounded_rectangle([tx, oy + 8, tx + tw, oy + 23], 4,
                                   fill=(238, 244, 253), outline=(216, 231, 250))
            canvas.text(tx + 6, oy + 10, tag, 10, False, color)
            tx += tw + 6
        canvas.right(ox + cell_w - 10, oy + 9,
                     f"{order['seated']}/{order['total_passengers']} 人就座",
                     10.5, False, MUTED)
        ly = oy + 28
        if order.get("base_desc"):
            canvas.text(ox + 10, ly, order["base_desc"], 10.5, False, MUTED)
            ly += 15
        if order.get("reason"):
            canvas.text(ox + 10, ly, "未出票：" + order["reason"][:30], 10.5,
                        False, BAD)
            ly += 15
        chips = [f"{p['name']} {p['seat_id'] or '未出票'}"
                 for p in order.get("passengers", [])][:4]
        cx = ox + 10
        for chip in chips:
            cw = int(draw.textlength(chip, font=font(10))) + 12
            if cx + cw > ox + cell_w - 10:
                break
            fill, edge, fg = ((253, 242, 241), (240, 216, 213), (179, 53, 47)) \
                if chip.endswith("未出票") else ((238, 244, 253), (216, 231, 250),
                                                 (47, 122, 208))
            draw.rounded_rectangle([cx, ly, cx + cw, ly + 15], 4, fill=fill,
                                   outline=edge)
            canvas.text(cx + 6, ly + 1, chip, 10, False, fg)
            cx += cw + 5
    y += record_h + GAP

    # 结论：逐行量宽后再定卡片高度，避免文字压出卡片
    # （第一版写死 96px，第三行被裁掉了）。
    lines = [
        "① 座位图从 1080px 左列改为占满整宽，16 节车厢由竖排 16 行改为并排 "
        + str(rows_of_cars) + " 行，一屏看完。",
        "② 车厢宽度按 minmax(250px,1fr) 自动计算：实测一行 " + str(cols)
        + f" 节、列宽 {col_w}px；座位网格用 width:max-content，只占 159px 不摊开。",
        "③ 提交订单面板占整行：基础分组与特殊人群并排，"
        "年龄段用 minmax(128px,1fr) 网格自动排齐。",
        "④ 【新】开发者提交的订单会记进台账 —— 原先这条路径完全没记录，"
        "「下单记录」永远是空的。",
        "⑤ 【新】订单号按台账已有单数续号：原先每次都是 COMP-1，三张不同的单同名。",
    ]
    box_h = 36 + len(lines) * 19 + 12
    canvas.card(22, y, W - 44, box_h, fill=(240, 251, 244),
                outline=(200, 232, 212))
    canvas.text(42, y + 12, "这次改了什么", 13.5, True, (33, 122, 69))
    for li, line in enumerate(lines):
        canvas.text(42, y + 36 + li * 19, line, 11.5, False, INK)
    y += box_h

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.img.crop((0, 0, W, y + 22)).save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB")
    print(f"内容区 {content_w}px｜每行 {cols} 节｜列宽 {col_w}px｜"
          f"共 {len(cars)} 节 / {rows_of_cars} 行")
    print(f"已售 {snapshot['occupied_count']} 座｜"
          f"余票 {sum(snapshot['remaining'].values())}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制开发者页新布局")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
