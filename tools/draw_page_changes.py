"""画两个页面改动后的样子：开发者页「提交订单」与用户模式人群选择两层。

用法::

    python tools/draw_page_changes.py

数据取自运行中的服务（真实乘客名与真实出票结果），用 Pillow 重绘布局。
受限环境无法无头截图，原理见 tools/README.md。
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

DEFAULT_OUT = ROOT / "docs" / "screenshots" / "page-changes.png"
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
DARK = (43, 58, 77)
GRAY = (150, 165, 182)


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def call(path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
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

    def card(self, x, y, w, h, fill=CARD, outline=None, width=1):
        self.draw.rounded_rectangle([x, y, x + w, y + h], 9, fill=fill,
                                    outline=outline, width=width)


def main() -> int:
    call("/api/dev/reset", {"passengers": True})
    profiles = call("/api/dev/passengers")["passengers"]
    catalog = call("/api/passengers/types")
    basic = [g for g in catalog["groups"] if g.get("basic")][0]
    special = [g for g in catalog["groups"] if not g.get("basic")]

    # 真实提交一单：成人 + 儿童 + 轮椅 + 照护人
    pick = []
    for want in ("adult", "child", "wheelchair", "caregiver"):
        hit = next((p for p in profiles if p["type_id"] == want), None)
        if hit:
            pick.append(hit)
    booked = call("/api/tickets/book", {
        "class_code": "二等座",
        "profile_ids": [p["profile_id"] for p in pick],
        "order_id": "DEMO-1",
    })

    W = 1560
    canvas = Canvas(W, 1500)
    draw = canvas.draw

    draw.rectangle([0, 0, W, 92], fill=DARK)
    canvas.text(32, 14, "本轮页面改动", 24, True, (255, 255, 255))
    canvas.text(32, 52,
                "① 批量提交页已删除，下单入口进开发者页   "
                "② 人群选择改为两层：常用 + 需要特别服务", 14, False, (190, 205, 220))
    canvas.text(W - 470, 20, "GET /booking → 404（页面与路由均已删除）",
                13, True, (140, 220, 170))

    y = 112
    # ---------- ① 开发者页「提交订单」----------
    panel_w = (W - 60) // 2
    # 面板高度要放得下"乘车人列表 + 提交按钮 + 出票结果"，
    # 第一版只给了 578，出票结果被卡片下边缘裁掉了。
    panel_h = 720
    canvas.card(22, y, panel_w, panel_h)
    canvas.text(42, y + 14, "开发者模式 /dev · 提交订单", 16, True, INK)
    canvas.text(42, y + 40, "勾选预制乘车人 → 选席别 → 提交", 12, False, MUTED)

    rx = 42
    ry = y + 66
    canvas.text(rx, ry + 3, "席别", 12, False, MUTED)
    draw.rounded_rectangle([rx + 40, ry, rx + 168, ry + 26], 6, fill=(251, 252, 253),
                           outline=LINE)
    canvas.text(rx + 52, ry + 5, "二等座", 12.5, False, INK)
    canvas.text(rx + 186, ry + 3, "单号", 12, False, MUTED)
    draw.rounded_rectangle([rx + 226, ry, rx + 448, ry + 26], 6, fill=(251, 252, 253),
                           outline=LINE)
    canvas.text(rx + 238, ry + 5, "留空自动生成", 12.5, False, MUTED)

    ry += 38
    canvas.text(rx, ry + 2, "已选 4 位", 12, True, BLUE)
    canvas.text(rx + 90, ry + 2, "全选 / 清空", 12, False, MUTED)

    ry += 24
    for item in profiles[:12]:
        checked = item in pick
        draw.rounded_rectangle([rx, ry, rx + panel_w - 62, ry + 26], 5,
                               fill=(242, 247, 253) if checked else CARD)
        box = (rx + 9, ry + 7, rx + 21, ry + 19)
        draw.rounded_rectangle(box, 3, fill=BLUE if checked else CARD,
                               outline=BLUE if checked else GRAY,
                               width=2 if checked else 1)
        if checked:
            draw.line([rx + 12, ry + 13, rx + 15, ry + 16, rx + 19, ry + 9],
                      fill=(255, 255, 255), width=2)
        canvas.text(rx + 30, ry + 5, item["name"], 12.5, True, INK)
        tag_w = int(draw.textlength(item["type_label"], font=font(11))) + 16
        draw.rounded_rectangle([rx + 96, ry + 5, rx + 96 + tag_w, ry + 21], 4,
                               fill=(238, 244, 253), outline=(216, 231, 250))
        canvas.text(rx + 104, ry + 7, item["type_label"], 11, False, (47, 122, 208))
        canvas.text(rx + 116 + tag_w, ry + 7, item.get("id_card") or "", 11, False, MUTED)
        ry += 30

    ry += 4
    draw.rounded_rectangle([rx, ry, rx + 116, ry + 30], 6, fill=BLUE)
    canvas.text(rx + 30, ry + 7, "提交订单", 13, True, (255, 255, 255))
    canvas.text(rx + 128, ry + 8, "同一订单默认坐一起；凑不出连座时自动分票",
                11.5, False, MUTED)

    ry += 44
    result = booked.get("order", {})
    seated = result.get("seated", 0)
    canvas.text(rx, ry, f"订单 DEMO-1 · 已出票 · "
                        f"{seated}/{len(result.get('passengers', []))} 人就座",
                12.5, True, OK if booked.get("ok") else BAD)
    ry += 22
    for item in (result.get("passengers") or [])[:4]:
        canvas.text(rx + 8, ry, item["name"], 12, False, INK)
        seat = item.get("seat_id") or "候补"
        is_bay = bool(item.get("wheelchair_bay"))
        draw.rounded_rectangle([rx + 96, ry - 2, rx + 198, ry + 20], 4,
                               fill=(230, 247, 247) if is_bay else (240, 245, 250))
        canvas.text(rx + 106, ry + 1, seat, 12, True,
                    (15, 125, 125) if is_bay else INK)
        canvas.text(rx + 210, ry + 1,
                    "轮椅固定停放位（独立编号）" if is_bay else item.get("class_code", ""),
                    11, False, MUTED)
        ry += 26

    # ---------- ② 用户模式人群选择两层 ----------
    x2 = 22 + panel_w + 16
    canvas.card(x2, y, panel_w, panel_h)
    canvas.text(x2 + 20, y + 14, "用户模式 /ticket · 添加乘车人", 16, True, INK)
    canvas.text(x2 + 20, y + 40, "普通旅客只看得到常用档；需要特别服务才展开",
                12, False, MUTED)

    by = y + 68
    canvas.text(x2 + 20, by, f"{basic['label']} · {basic['hint']}", 11.5, False, MUTED)
    by += 20
    bx = x2 + 20
    cell = (panel_w - 52) // 2
    for index, item in enumerate(basic["types"]):
        col = index % 2
        row = index // 2
        cx = bx + col * (cell + 8)
        cy = by + row * 62
        selected = item["id"] == "adult"
        draw.rounded_rectangle([cx, cy, cx + cell, cy + 54], 7,
                               fill=(247, 251, 255) if selected else CARD,
                               outline=BLUE if selected else LINE,
                               width=2 if selected else 1)
        canvas.text(cx + 12, cy + 8, item["label"], 13.5, True, INK)
        canvas.text(cx + 12, cy + 28, item["desc"][:22], 10.5, False, MUTED)
    by += 2 * 62 + 6

    # 折叠区
    draw.rounded_rectangle([x2 + 20, by, x2 + panel_w - 20, by + 34], 7,
                           fill=(247, 249, 252), outline=LINE)
    canvas.text(x2 + 34, by + 8,
                "需要特别服务（孕妇 / 残疾 / 陪同人 / 未成年细分）", 12.5, False,
                (59, 74, 90))
    canvas.right(x2 + panel_w - 34, by + 8, "▴", 13, True, MUTED)
    by += 34 + 12

    for group in special:
        canvas.text(x2 + 20, by, group["label"], 12, True, INK)
        canvas.text(x2 + 20 + 70, by + 1, group["hint"][:30], 10.5, False, MUTED)
        by += 20
        gx = x2 + 20
        for item in group["types"]:
            label = item["label"]
            width = int(draw.textlength(label, font=font(11.5))) + 30
            if gx + width > x2 + panel_w - 24:
                gx = x2 + 20
                by += 26
            draw.rounded_rectangle([gx, by, gx + width, by + 22], 5,
                                   fill=(243, 246, 250), outline=LINE)
            canvas.text(gx + 15, by + 3, label, 11.5, False, (70, 84, 100))
            gx += width + 8
        by += 30

    canvas.text(x2 + 20, by + 4,
                "孕妇按孕周、残疾按类别确实需要细分，但它们是第二层。",
                11.5, False, MUTED)
    canvas.text(x2 + 20, by + 22,
                "「成人」旁边不再并列「孕妇（4-6个月）」。", 11.5, False, MUTED)

    y += panel_h + 16
    # ---------- 结论条 ----------
    canvas.card(22, y, W - 44, 92, fill=(240, 251, 244))
    canvas.text(42, y + 14, "改动前后", 14, True, (33, 122, 69))
    rows = [
        ("批量提交页", "/booking（87 KB 页面 + 两个路由）", "已删除，返回 404"),
        ("下单入口", "分散在 /booking 工具页", "/dev 顶部「提交订单」面板"),
        ("人群选择", "16 种平铺（含孕妇 4 档、视障 2 种）",
         "常用 4 种平铺 + 特别服务折叠区"),
        ("接口能力", "/api/orders/submit", "保留，改由接口级验收守护"),
    ]
    ry2 = y + 40
    for left, before, after in rows:
        canvas.text(42, ry2, left, 11.5, True, MUTED)
        canvas.text(160, ry2, before, 11.5, False, (150, 90, 88))
        canvas.text(640, ry2, "→", 12, True, MUTED)
        canvas.text(680, ry2, after, 11.5, False, (33, 122, 69))
        ry2 += 15

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.img.crop((0, 0, W, y + 92 + 24)).save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB")
    print(f"预置乘车人 {len(profiles)} 位｜常用档 {len(basic['types'])} 种"
          f"｜特别服务 {len(special)} 组")
    print(f"出票结果：{[(p['name'], p['seat_id']) for p in result.get('passengers', [])]}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制页面改动示意")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
