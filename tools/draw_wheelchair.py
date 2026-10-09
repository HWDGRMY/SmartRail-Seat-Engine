"""画出无障碍轮椅停放位的独立编号与"询问后出票"流程（真实数据）。

用法::

    python tools/draw_wheelchair.py

数据取自运行中的服务；用 Pillow 重绘界面布局（受限环境无法无头截图，
原理见 tools/README.md）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "docs" / "screenshots" / "wheelchair-bays.png"
BASE = "http://127.0.0.1:8000"
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

BG = (242, 244, 247)
CARD = (255, 255, 255)
INK = (51, 51, 51)
MUTED = (123, 135, 148)
LINE = (233, 237, 242)
BLUE = (59, 142, 234)
OK = (46, 171, 91)
WARN = (230, 162, 60)
BAD = (229, 80, 74)
TEAL = (15, 125, 125)
TEAL_BG = (230, 247, 247)
DARK = (43, 58, 77)


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def call(path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
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

    def right(self, right, y, content, size=12, bold=False, fill=INK):
        width = self.draw.textlength(content, font=font(size, bold))
        self.draw.text((right - width, y), content, font=font(size, bold), fill=fill)

    def tag(self, x, y, content, fg=BLUE, bg=(238, 244, 253), size=11):
        width = self.draw.textlength(content, font=font(size)) + 14
        self.draw.rounded_rectangle([x, y, x + width, y + 18], 4, fill=bg)
        self.draw.text((x + 7, y + 2), content, font=font(size), fill=fg)
        return width


def main() -> int:
    # 干净状态，然后连下 5 张轮椅单（第 5 张触发询问）
    call("/api/dev/reset", {"passengers": True})
    passengers = call("/api/dev/passengers")["passengers"]
    wheel = next(p for p in passengers if p["type_id"] == "wheelchair")
    plain = next(p for p in passengers if p["type_id"] == "adult")

    before_seats = call("/api/dev/snapshot")["remaining"]["二等座"]
    rows: list[dict] = []
    for index in range(1, 6):
        verdict = call("/api/trains/evaluate",
                       {"class_code": "二等座", "profile_ids": [wheel["profile_id"]]})
        booked = call("/api/tickets/book",
                      {"class_code": "二等座", "profile_ids": [wheel["profile_id"]],
                       "order_id": f"W{index}"})
        payload = booked["order"]["passengers"][0]
        rows.append({
            "index": index,
            "seat_id": payload["seat_id"],
            "bay": payload.get("wheelchair_bay", ""),
            "internal": payload.get("internal_seat_id", ""),
            "assigned": bool(payload.get("bay_assigned")),
            "ask": verdict["wheelchair"],
        })
    snapshot = call("/api/dev/snapshot")
    bays = snapshot["wheelchair_bays"]
    after_seats = snapshot["remaining"]["二等座"]
    # 再下一张普通旅客单，用来说明"座位票额"是另一条账
    plain_booked = call("/api/tickets/book",
                        {"class_code": "二等座", "profile_ids": [plain["profile_id"]],
                         "order_id": "P1"})
    plain_seat = plain_booked["order"]["passengers"][0]["seat_id"]

    W = 1500
    canvas = Canvas(W, 2400)
    draw = canvas.draw
    y = 0

    draw.rectangle([0, y, W, y + 84], fill=TEAL)
    canvas.text(32, y + 14, "无障碍轮椅固定停放位 · 完全独立编号", 23, True, (255, 255, 255))
    canvas.text(32, y + 50,
                "04 车与 12 车各 2 个，全列 4 个；不占座位票额；"
                "满位后先询问，确认再出普通坐票", 13, False, (215, 240, 240))
    y += 84 + 16

    # ---------- 1. 停放位总览 ----------
    canvas.panel(22, y, W - 44, 128)
    canvas.text(40, y + 12, "停放位总览", 15, True, INK)
    canvas.right(W - 40, y + 14,
                 f"共 {bays['total']} 个 · 空闲 {bays['free']} · 已占 {bays['occupied']}",
                 13, True, TEAL)
    bx = 40
    for bay in bays["bays"]:
        occupied = bay["occupied"]
        draw.rounded_rectangle([bx, y + 44, bx + 208, y + 108], 8,
                               fill=(253, 242, 241) if occupied else TEAL_BG,
                               outline=BAD if occupied else TEAL,
                               width=2 if occupied else 1)
        canvas.text(bx + 14, y + 52, bay["bay_id"], 17, True,
                    BAD if occupied else TEAL)
        canvas.text(bx + 14, y + 76,
                    f"{bay['carriage']:02d} 车 · 独立编号", 11, False, MUTED)
        canvas.text(bx + 14, y + 90,
                    bay.get("passenger_name") or "空闲", 11.5, False,
                    BAD if occupied else (110, 120, 132))
        bx += 220
    canvas.text(40, y + 112, bays["note"], 11, False, MUTED)
    y += 128 + 14

    # ---------- 2. 逐单分配 ----------
    row_h = 62
    panel_h = 50 + len(rows) * row_h
    canvas.panel(22, y, W - 44, panel_h)
    canvas.text(40, y + 12, "连续 5 张轮椅订单的分配结果", 15, True, INK)
    canvas.right(W - 40, y + 14,
                 "前 4 张拿到停放位，第 5 张先询问再出普通坐票", 11.5, False, MUTED)
    ry = y + 46
    for row in rows:
        assigned = row["assigned"]
        color = TEAL if assigned else WARN
        draw.rectangle([40, ry, 44, ry + row_h - 12], fill=color)
        canvas.text(58, ry + 2, f"第 {row['index']} 张", 12.5, True, INK)
        # 票面
        canvas.text(132, ry + 2, "票面", 11, False, MUTED)
        draw.rounded_rectangle([166, ry - 2, 300, ry + 26], 6,
                               fill=TEAL_BG if assigned else (253, 248, 240),
                               outline=color)
        canvas.text(178, ry + 3, row["seat_id"], 14, True, color)
        # 说明
        if assigned:
            canvas.text(320, ry + 4,
                        f"分配到轮椅停放位 {row['bay']}"
                        f"（内部落点 {row['internal']}，仅工程侧记录）",
                        11.5, False, (85, 95, 110))
        else:
            ask = row["ask"]
            canvas.text(320, ry + 4,
                        f"停放位已满（0 个）→ 询问后改出普通坐票"
                        f"（{row['seat_id']}）",
                        11.5, False, WARN)
        # 询问原文（第 5 张）
        if not assigned and row["ask"].get("question"):
            canvas.text(320, ry + 24, row["ask"]["question"][:104] + "…",
                        10.5, False, MUTED)
        elif assigned:
            canvas.text(320, ry + 24,
                        "无需询问：停放位可用，直接按停放位编号出票",
                        10.5, False, MUTED)
        ry += row_h
    y += panel_h + 14

    # ---------- 3. 座位票额是另一条账 ----------
    canvas.panel(22, y, W - 44, 132)
    canvas.text(40, y + 12, "为什么不占座位票额", 15, True, INK)
    items = [
        ("初始二等座余票", str(before_seats), INK),
        ("4 位轮椅旅客后", str(after_seats), OK),
        ("差额", f"{before_seats - after_seats}", OK),
        ("再卖 1 张普通票", plain_seat, BLUE),
    ]
    cx = 40
    for label, value, color in items:
        canvas.text(cx, y + 44, label, 11.5, False, MUTED)
        canvas.text(cx, y + 62, value, 22, True, color)
        cx += 250
    canvas.text(40, y + 98,
                "4 位轮椅旅客全部拿到停放位，但二等座余票一个都没减少 —— "
                "停放位是独立资源、独立编号，不吃座位票额。",
                12, False, INK)
    canvas.text(40, y + 116,
                "只有改出普通坐票的那位（第 5 张）才占用 1 个座位。",
                11.5, False, MUTED)
    y += 132 + 14

    # ---------- 4. 座位图上的呈现 ----------
    canvas.panel(22, y, W - 44, 168)
    canvas.text(40, y + 12, "开发者座位图上的呈现", 15, True, INK)
    canvas.text(40, y + 36,
                "停放位单独列出（不在座位表里）；座位表只把紧邻的座位标成"
                "「近轮椅位」，它们仍是可售的普通座位。", 11.5, False, MUTED)
    lx = 40
    for label, fill, outline in (
        ("普通可售座位", (255, 255, 255), (207, 214, 222)),
        ("紧邻轮椅位（仍可售）", TEAL_BG, TEAL),
        ("用户已售", (159, 178, 198), (142, 163, 186)),
        ("静音车厢", (255, 255, 255), (109, 91, 208)),
    ):
        draw.rounded_rectangle([lx, y + 62, lx + 30, y + 84], 4,
                               fill=fill, outline=outline, width=2)
        canvas.text(lx + 38, y + 66, label, 11.5, False, INK)
        # 步长按标签实际宽度算，避免固定间距导致参差不齐
        lx += 38 + int(draw.textlength(label, font=font(11.5))) + 34
    canvas.text(40, y + 100,
                "04 车 · 二等座 78 座 + 轮椅停放位 2 个"
                "（座位表 78 行座位，停放位不占其中之一）", 12, True, INK)
    canvas.text(40, y + 124,
                "12 车 · 二等座 78 座 + 轮椅停放位 2 个", 12, True, INK)
    canvas.text(40, y + 146,
                f"全列：座位 1238 个 + 停放位 4 个（两个计数器互不影响）",
                11.5, False, MUTED)
    y += 168 + 16

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    img = canvas.img.crop((0, 0, W, min(y, 2400)))
    img.save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB  {img.size}")
    print(f"停放位 {bays['total']} 个｜二等座 {before_seats} -> {after_seats}"
          f"（轮椅 5 张后）｜普通票 {plain_seat}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制轮椅停放位分配示意图")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help=f"输出 PNG（默认 {DEFAULT_OUT.relative_to(ROOT)}）")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
