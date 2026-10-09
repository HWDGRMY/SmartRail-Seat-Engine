"""画开发者页组单面板的**新布局**（真实数据，Pillow 重绘）。

用法::

    python tools/draw_page_changes.py

图上要能看出两件事：
  ① 基础分组（需求给的 5 类）用加减数量，特殊人群是叠加维度；
  ② 之前"轻/度/残/疾竖成一列"的排版问题已修（标签不换行，
     年龄段控件横排在标题下方）。
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
PANEL = (251, 252, 253)


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

    def button(self, x, y, label, w=26, h=22, fill=PANEL, fg=INK, bold=False):
        self.draw.rounded_rectangle([x, y, x + w, y + h], 5, fill=fill,
                                    outline=LINE)
        size = font(12.5, bold)
        tw = self.draw.textlength(label, font=size)
        self.draw.text((x + (w - tw) / 2, y + (h - 15) / 2), label,
                       font=size, fill=fg)


def stepper_row(canvas, x, y, width, label, desc, value):
    """一行"名称 + 说明 + [− n +]"，标签**不换行**。"""
    canvas.text(x, y, label, 12.5, True, INK)
    canvas.text(x, y + 15, desc, 10.5, False, MUTED)
    ctl = x + width - 92
    canvas.button(ctl, y, "−", fg=MUTED if value == 0 else INK)
    canvas.text(ctl + 34, y + 2, str(value), 13, True, INK)
    canvas.button(ctl + 60, y, "+")
    return y + 36


def band_row(canvas, x, y, title, hint, band_labels, prefix):
    """特殊人群一行：标题一行，年龄段控件横排在第二行。

    hint 的位置按**标题实测宽度**推出来 —— 写死偏移会在
    「孕10个月/37周+」这种长标题上被压住（第一版就是这样重叠的）。
    """
    canvas.text(x, y, title, 12.5, True, INK)
    title_w = canvas.draw.textlength(title, font=font(12.5, True))
    canvas.text(x + title_w + 10, y + 1, hint, 10.5, False, MUTED)
    gx = x
    gy = y + 19
    for name in band_labels:
        name_w = int(canvas.draw.textlength(name, font=font(10.5))) + 8
        cell_w = name_w + 26 + 24 + 26
        canvas.draw.rounded_rectangle([gx, gy, gx + cell_w, gy + 24], 4,
                                      fill=(250, 251, 253))
        canvas.text(gx + 5, gy + 5, name, 10.5, False, MUTED)
        canvas.button(gx + name_w + 2, gy + 1, "−", w=24, h=22, fg=MUTED)
        canvas.text(gx + name_w + 30, gy + 3, "0", 12, True, INK)
        canvas.button(gx + name_w + 50, gy + 1, "+", w=24, h=22)
        gx += cell_w + 6
    return gy + 34


def main() -> int:
    call("/api/dev/reset", {"passengers": True})
    profiles = call("/api/dev/passengers")["passengers"]
    schema = call("/api/composition/schema")
    bands = [b["id"] for b in schema["age_bands"]]
    band_labels = [b["label"] for b in schema["age_bands"]]

    def blank():
        return {
            "note": "演示",
            "base": {g["id"]: 0 for g in schema["base_groups"]},
            "child_sub": {g["id"]: 0 for g in schema["child_sub_groups"]},
            "disability": {lv["id"]: {b: 0 for b in bands}
                           for lv in schema["disability_levels"]},
            "pregnant": {st["id"]: {b: 0 for b in bands}
                         for st in schema["pregnant_stages"]},
        }

    # 真实提交：3 成人 + 2 儿童（其中 1 位吵闹儿童）
    payload = blank()
    payload["base"]["adult"] = 3
    payload["base"]["child"] = 2
    payload["child_sub"]["child_noisy"] = 1
    booked = call("/api/composition/submit", {"orders": [payload]})
    order = (booked.get("orders") or [{}])[0]
    seats = sorted((order.get("seats") or {}).values())

    # 空提交：演示"给出人话原因"而不是内部报错
    guarded = call("/api/composition/submit", {"orders": [blank()]})
    blocked = (guarded.get("blocked") or [{}])[0]

    W = 1560
    canvas = Canvas(W, 1500)
    draw = canvas.draw
    draw.rectangle([0, 0, W, 88], fill=DARK)
    canvas.text(32, 12, "开发者页组单面板（新布局）", 23, True, (255, 255, 255))
    canvas.text(32, 50, "基础分组 5 类按人数加减；特殊人群是叠加维度，"
                        "标签不换行、年龄段横排", 13.5, False, (190, 205, 220))

    y = 106
    panel_w = 560
    panel_h = 1180
    canvas.card(22, y, panel_w, panel_h)
    canvas.text(42, y + 14, "提交订单", 15.5, True, INK)
    canvas.text(118, y + 16, "按基础分组加人数 → 需要时叠加特殊人群 → 提交",
                11.5, False, MUTED)

    x = 42
    width = panel_w - 40
    ry = y + 52
    canvas.text(x, ry + 4, "席别", 12, False, MUTED)
    draw.rounded_rectangle([x + 40, ry, x + 168, ry + 28], 6, fill=PANEL,
                           outline=LINE)
    canvas.text(x + 52, ry + 6, "二等座", 12.5, False, INK)
    canvas.button(x + 182, ry + 3, "清空", w=52, h=23)
    ry += 46

    canvas.text(x, ry, "基础分组", 13, True, (59, 74, 90))
    canvas.text(x + 74, ry + 1, "总人数只由这 5 类决定", 11, False, MUTED)
    canvas.right(x + width, ry + 1, "共 5 人", 12.5, True, BLUE)
    ry += 22
    for group in schema["base_groups"]:
        value = payload["base"][group["id"]]
        draw.rectangle([x, ry + 3, x + 3, ry + 21], fill=BLUE if value else LINE)
        ry = stepper_row(canvas, x + 12, ry, width - 12, group["label"],
                         group["desc"], value)

    ry += 10
    draw.rounded_rectangle([x, ry, x + width, ry + 32], 7,
                           fill=(247, 249, 252), outline=LINE)
    canvas.text(x + 14, ry + 7, "特殊人群", 12.5, True, (59, 74, 90))
    canvas.text(x + 76, ry + 8, "叠加在基础分组之上，不改变总人数", 11, False, MUTED)
    canvas.right(x + width - 14, ry + 7, "收起 ▴", 11.5, False, MUTED)
    ry += 44

    canvas.text(x, ry, "儿童细分", 12, True, INK)
    canvas.text(x + 62, ry + 1, "叠加在「儿童」之上", 10.5, False, MUTED)
    ry += 19
    # 儿童细分：值放在按钮**右侧**固定位置，说明另起一列，
    # 避免"儿童（4-14）（安静）儿童 4-14 岁"这种贴在一起。
    for group in schema["child_sub_groups"]:
        value = payload["child_sub"][group["id"]]
        canvas.button(x, ry, "−", w=24, h=22, fg=MUTED if value == 0 else INK)
        canvas.text(x + 30, ry + 3, str(value), 12, True, INK)
        canvas.button(x + 50, ry, "+", w=24, h=22)
        canvas.text(x + 88, ry + 4, group["label"], 12, False, INK)
        canvas.text(x + 88 + int(canvas.draw.textlength(
            group["label"], font=font(12))) + 14, ry + 5, "儿童 4-14 岁",
            10.5, False, MUTED)
        ry += 27
    ry += 8

    canvas.text(x, ry, "残疾旅客", 12, True, INK)
    canvas.text(x + 62, ry + 1, "程度 × 年龄段（按年龄段叠加）", 10.5, False, MUTED)
    ry += 21
    for level in schema["disability_levels"]:
        hint = ("需重点旅客服务" if level["requires_key_service"]
                else "无需重点服务")
        if level["requires_companion"]:
            hint += " · 需陪同"
        ry = band_row(canvas, x, ry, f"{level['label']}残疾", hint, band_labels,
                      f"disability-{level['id']}")
    ry += 6

    canvas.text(x, ry, "孕妇", 12, True, INK)
    canvas.text(x + 40, ry + 1, "孕期 × 年龄段", 10.5, False, MUTED)
    ry += 21
    for stage in schema["pregnant_stages"]:
        hint = ("需重点旅客服务" if stage["requires_key_service"]
                else "无需重点服务")
        if stage["requires_companion"]:
            hint += " · 需陪同"
        if stage["configurable"]:
            hint += " · 平台可配置"
        ry = band_row(canvas, x, ry, f"孕{stage['label']}", hint, band_labels,
                      f"pregnant-{stage['id']}")

    ry += 8
    canvas.button(x, ry, "提交订单", w=104, h=30, fill=BLUE,
                  fg=(255, 255, 255), bold=True)
    canvas.text(x + 118, ry + 8, "同一订单默认坐一起；凑不出连座时自动分票",
                11, False, MUTED)
    ry += 42
    canvas.text(x, ry, f"订单 COMP-1 · 总人数 {order.get('total_passengers', 0)}"
                       f" · 出票 {len(seats)}", 12.5, True, OK)
    ry += 20
    canvas.text(x, ry, "座位：" + " ".join(seats), 11.5, False, INK)
    ry += 18
    canvas.text(x, ry, "（3 成人 + 2 儿童 → 02 车第 1 排，其中 1 位标为吵闹儿童）",
                10.5, False, MUTED)

    # ---------------- 右：口径与修复 ----------------
    x2 = 22 + panel_w + 18
    w2 = W - x2 - 22
    canvas.card(x2, y, w2, 460)
    canvas.text(x2 + 20, y + 14, "基础分组 = 需求给定的 5 类", 15.5, True, INK)
    canvas.text(x2 + 20, y + 40,
                "与 smartrail/composition.py 的 BASE_GROUP_FIELDS 完全一致",
                11.5, False, MUTED)
    by = y + 68
    for item in schema["base_groups"]:
        draw.rounded_rectangle([x2 + 20, by, x2 + 220, by + 34], 6,
                               fill=(247, 251, 255), outline=(216, 231, 250))
        canvas.text(x2 + 32, by + 3, item["label"], 13, True, INK)
        canvas.text(x2 + 32, by + 19, item["desc"], 10.5, False, MUTED)
        canvas.text(x2 + 244, by + 8, f"id: '{item['id']}'", 11.5, False,
                    (47, 122, 208))
        by += 40
    by += 8
    canvas.text(x2 + 20, by, f"总人数公式：{schema['total_formula']}", 12.5, True, INK)
    by += 22
    canvas.text(x2 + 20, by, "不计入总人数的叠加维度："
                + "、".join(schema["excluded_from_total"]), 11.5, False, MUTED)
    by += 22
    canvas.text(x2 + 20, by, f"预制乘车人：{len(profiles)} 位（仅开发者模式可见）",
                11.5, False, MUTED)

    by2 = y + 478
    fix_h = 330
    canvas.card(x2, by2, w2, fix_h, fill=(253, 246, 242), outline=(240, 214, 200))
    canvas.text(x2 + 20, by2 + 14, "这次修掉的问题", 14, True, (168, 92, 60))
    lines = [
        ("① 排版难看", "特殊人群标签被压成「轻/度/残/疾」竖成一列。"),
        ("", "根因：.sname 是 flex-direction:column 且没有 min-width:0，"),
        ("", "长标签被挤到逐字换行；年龄段控件也因宽度不足挤成一团。"),
        ("", "已改为 CSS Grid：标签不换行（ellipsis），年龄段横排"),
        ("", "在标题下方；右栏宽度 380 → 440，年龄段控件独立窄样式。"),
        ("", ""),
        ("② 提交报错", "「提交失败：Cannot read properties of undefined"),
        ("", "(reading 'errors')」—— 前端读的是 blocked.check.errors，"),
        ("", "而真实结构是 blocked[0].errors（errors 在顶层）。"),
        ("", "现在读顶层并做兜底；空提交显示「订单中没有乘客」。"),
        ("", ""),
        ("③ 状态不一致", "基础分组减小时，儿童细分可能超过儿童人数。"),
        ("", "已加 clampChildSub()，自动收回到上限内。"),
    ]
    ly = by2 + 42
    for head, body in lines:
        if head:
            canvas.text(x2 + 20, ly, head, 11.5, True, (168, 92, 60))
        canvas.text(x2 + 132, ly, body, 11.5, False, (120, 74, 52))
        ly += 20

    by3 = by2 + fix_h + 14
    canvas.card(x2, by3, w2, 120, fill=(240, 251, 244), outline=(200, 232, 212))
    canvas.text(x2 + 20, by3 + 12, "空提交时的实测提示（不再是内部报错）", 12.5,
                True, (33, 122, 69))
    canvas.text(x2 + 20, by3 + 40,
                "被拦下（未进入求解器）："
                + "；".join(blocked.get("errors") or []), 11.5, False, INK)
    canvas.text(x2 + 20, by3 + 64,
                "拒票规则仍在组单阶段生效：未满 14 岁无成人陪同、",
                11, False, MUTED)
    canvas.text(x2 + 20, by3 + 82,
                "重度/极重度残疾未约重点旅客服务，均不予出票。", 11, False, MUTED)

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.img.crop((0, 0, W, max(y + panel_h, by3 + 120) + 22)).save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB")
    print(f"基础分组 {len(schema['base_groups'])} 类｜"
          f"叠加维度 {len(schema['excluded_from_total'])} 个｜"
          f"预制 {len(profiles)} 位")
    print(f"3 成人 + 2 儿童 出票：{seats}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制开发者页组单面板新布局")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
