"""画开发者页的"按基础分组加人数"组单方式（真实数据）。

用法::

    python tools/draw_page_changes.py

这张图回答一个问题：**为什么按下单页是加减人数而不是勾选姓名。**
人名对下单没有意义；而且基础分组只有需求给定的 5 类，
特殊人群（孕妇/残疾/儿童细分）是叠加维度，不计入总人数。
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
DARK = (43, 58, 77)
CLAY = (168, 92, 60)


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


def composition(schema: dict, base: dict, disability=None, pregnant=None) -> dict:
    bands = [b["id"] for b in schema["age_bands"]]
    return {
        "note": "验收",
        "base": base,
        "child_sub": {g["id"]: 0 for g in schema["child_sub_groups"]},
        "disability": {
            lv["id"]: {b: (disability or {}).get(lv["id"], {}).get(b, 0)
                       for b in bands}
            for lv in schema["disability_levels"]
        },
        "pregnant": {
            st["id"]: {b: (pregnant or {}).get(st["id"], {}).get(b, 0)
                       for b in bands}
            for st in schema["pregnant_stages"]
        },
    }


def main() -> int:
    call("/api/dev/reset", {"passengers": True})
    profiles = call("/api/dev/passengers")["passengers"]
    catalog = call("/api/passengers/types")
    schema = call("/api/composition/schema")
    special = [g for g in catalog["groups"] if not g.get("basic")]

    # 真实提交：10 位成人（演示"任意数量"）
    ten = composition(schema, {"adult": 10, "youth": 0, "child": 0,
                               "toddler": 0, "infant": 0})
    booked = call("/api/composition/submit", {"orders": [ten]})
    order = (booked.get("orders") or [{}])[0]
    seats = sorted((order.get("seats") or {}).values())

    # 真实被拦下的一单：重度残疾未约重点旅客服务
    guard = composition(schema, {"adult": 4},
                        disability={"severe": {"adult": 2}})
    guarded = call("/api/composition/submit", {"orders": [guard]})
    blocked_errors = (guarded.get("blocked") or [{}])[0].get("errors") or []

    W = 1560
    canvas = Canvas(W, 1200)
    draw = canvas.draw
    draw.rectangle([0, 0, W, 92], fill=DARK)
    canvas.text(32, 14, "开发者页组单：按基础分组加人数", 24, True, (255, 255, 255))
    canvas.text(32, 52,
                "人名对下单没有意义；基础分组只有需求给定的 5 类，"
                "特殊人群是叠加维度（不计入总人数）", 14, False, (190, 205, 220))

    y = 112
    panel_w = (W - 60) // 2
    # 面板要放得下：5 个基础分组 + 特殊人群 + 提交按钮 + 出票结果。
    # 先前给 660，提交按钮与结果被卡片下边缘裁掉了。
    panel_h = 760
    canvas.card(22, y, panel_w, panel_h)
    canvas.text(42, y + 14, "开发者模式 /dev · 提交订单", 16, True, INK)
    canvas.text(42, y + 40, "加减人数即可 —— 想加 10 位成人就点 10 次 +",
                12, False, MUTED)

    ry = y + 68
    canvas.text(42, ry + 3, "席别", 12, False, MUTED)
    draw.rounded_rectangle([82, ry, 210, ry + 26], 6, fill=(251, 252, 253),
                           outline=LINE)
    canvas.text(94, ry + 5, "二等座", 12.5, False, INK)
    draw.rounded_rectangle([226, ry, 322, ry + 26], 6, fill=(251, 252, 253),
                           outline=LINE)
    canvas.text(242, ry + 5, "清空", 12.5, False, MUTED)

    ctl = 22 + panel_w - 150
    ry += 42
    canvas.text(42, ry, "基础分组", 13, True, (59, 74, 90))
    canvas.right(22 + panel_w - 24, ry + 1, "共 10 人", 12.5, True, BLUE)
    ry += 22
    for item in schema["base_groups"]:
        value = ten["base"][item["id"]]
        draw.rectangle([42, ry + 2, 45, ry + 20], fill=BLUE if value else LINE)
        canvas.text(56, ry, item["label"], 12.5, True, INK)
        canvas.text(56, ry + 16, item["desc"], 10.5, False, MUTED)
        draw.rounded_rectangle([ctl, ry, ctl + 26, ry + 24], 5, fill=CARD,
                               outline=LINE)
        canvas.text(ctl + 10, ry + 4, "−", 13, True, MUTED if value == 0 else INK)
        canvas.text(ctl + 44, ry + 4, str(value), 14, True, INK)
        draw.rounded_rectangle([ctl + 70, ry, ctl + 96, ry + 24], 5, fill=CARD,
                               outline=LINE)
        canvas.text(ctl + 78, ry + 4, "+", 13, True, INK)
        ry += 40

    ry += 6
    draw.rounded_rectangle([42, ry, 22 + panel_w - 24, ry + 32], 7,
                           fill=(247, 249, 252), outline=LINE)
    canvas.text(56, ry + 7, "特殊人群 · 叠加在基础分组之上，不改变总人数",
                12, False, (59, 74, 90))
    canvas.right(22 + panel_w - 36, ry + 7, "▴", 13, True, MUTED)
    ry += 40
    for group in special:
        canvas.text(56, ry, group["label"], 12, True, INK)
        canvas.text(56 + 140, ry + 1, group["hint"][:26], 10.5, False, MUTED)
        ry += 18
        gx = 56
        for item in group["types"]:
            width = int(draw.textlength(item["label"], font=font(11))) + 26
            if gx + width > 22 + panel_w - 30:
                gx = 56
                ry += 24
            draw.rounded_rectangle([gx, ry, gx + width, ry + 20], 4,
                                   fill=(243, 246, 250), outline=LINE)
            canvas.text(gx + 13, ry + 2, item["label"], 11, False, (70, 84, 100))
            gx += width + 6
        ry += 28

    ry += 6
    draw.rounded_rectangle([42, ry, 158, ry + 30], 6, fill=BLUE)
    canvas.text(72, ry + 7, "提交订单", 13, True, (255, 255, 255))
    ry += 42
    canvas.text(42, ry,
                f"订单 COMP-1 · 总人数 {order.get('total_passengers', 0)}"
                f" · 出票 {len(seats)}/{len(seats)}", 12.5, True, OK)
    ry += 20
    canvas.text(42, ry, "座位：" + " ".join(seats[:10]), 11.5, False, INK)
    ry += 16
    canvas.text(42, ry, "（10 位成人 → 02 车第 1、2 排各 5 座，全部相邻）",
                11, False, MUTED)

    # ---------- 右：基础分组口径 ----------
    x2 = 22 + panel_w + 16
    canvas.card(x2, y, panel_w, panel_h)
    canvas.text(x2 + 20, y + 14, "基础分组 = 需求给定的 5 类", 16, True, INK)
    canvas.text(x2 + 20, y + 40,
                "与 smartrail/composition.py 的 BASE_GROUP_FIELDS 完全一致",
                12, False, MUTED)
    by = y + 72
    for item in schema["base_groups"]:
        draw.rounded_rectangle([x2 + 20, by, x2 + 220, by + 38], 6,
                               fill=(247, 251, 255), outline=(216, 231, 250))
        canvas.text(x2 + 34, by + 4, item["label"], 13.5, True, INK)
        canvas.text(x2 + 34, by + 21, item["desc"], 10.5, False, MUTED)
        canvas.text(x2 + 250, by + 10, f"id: '{item['id']}'", 11.5, False,
                    (47, 122, 208))
        by += 44

    by += 4
    canvas.text(x2 + 20, by, f"总人数公式：{schema['total_formula']}", 12.5, True, INK)
    by += 22
    canvas.text(x2 + 20, by, "不计入总人数的叠加维度："
                + "、".join(schema["excluded_from_total"]), 12, False, MUTED)
    by += 24

    correction_h = 150
    draw.rounded_rectangle([x2 + 20, by, x2 + panel_w - 20, by + correction_h], 8,
                           fill=(253, 246, 242), outline=(240, 214, 200))
    canvas.text(x2 + 36, by + 12, "这次纠正了什么", 13, True, CLAY)
    for index, line in enumerate([
        "① 我先前另立了一套 16 类，还自己加了「学生」当基础分组 ——",
        "    需求从未定义该分组。学生是票价属性（0.75），不是人群分类。",
        "② 先前把「孕妇（4-6个月）」「视障（需导盲）」与「成人」平铺在",
        "    同一层；它们应是叠加维度，不计入总人数。",
        "③ 先前按下单页是勾选姓名 —— 预制只有固定几个人，无法验证",
        "    「10 位成人」。现改为按分组加减人数。",
        "④ 每个类型现在带 base_group / special 字段，测试会机器校验",
        "    基础分组恰好是那 5 类、且没有「学生」基础分组。",
    ]):
        canvas.text(x2 + 36, by + 36 + index * 14, line, 11, False, (120, 74, 52))

    by += correction_h + 16
    draw.rounded_rectangle([x2 + 20, by, x2 + panel_w - 20, by + 86], 8,
                           fill=(240, 251, 244), outline=(200, 232, 212))
    canvas.text(x2 + 36, by + 12, "拒票规则仍然生效（需求要求）", 13, True,
                (33, 122, 69))
    canvas.text(x2 + 36, by + 36,
                "重度/极重度残疾必须预约重点旅客服务；未满 14 岁无成人陪同", 11.5,
                False, INK)
    canvas.text(x2 + 36, by + 54,
                "不予出票 —— 这类订单在组单阶段就被拦下，不进入求解器。", 11.5,
                False, INK)
    if blocked_errors:
        canvas.text(x2 + 36, by + 70, f"实测：{blocked_errors[0]}", 10.5, False,
                    (33, 122, 69))

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.img.crop((0, 0, W, y + panel_h + 24)).save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB")
    print(f"基础分组 {len(schema['base_groups'])} 类｜"
          f"叠加维度 {len(schema['excluded_from_total'])} 个｜"
          f"预制 {len(profiles)} 位")
    print(f"10 位成人出票：{seats}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制开发者页组单方式")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
