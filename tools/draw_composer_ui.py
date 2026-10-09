"""画出 OrderEditor 的**组件布局**（分组控件本身，而不只是数据表）。

用法::

    python tools/draw_composer_ui.py

数据取自 ``/api/composition/schema``（字段定义）与 ``/api/composition/check``
（校验结论），因此图上每个计数与提示都与界面实际显示一致。
受限环境无法无头截图（见 tools/README.md），故用 Pillow 重绘布局。
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

DEFAULT_OUT = ROOT / "docs" / "screenshots" / "order-editor-ui.png"
BASE = "http://127.0.0.1:8000"
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

BG = (242, 244, 247)
CARD = (255, 255, 255)
PANEL = (251, 252, 253)
INK = (51, 51, 51)
MUTED = (152, 162, 174)
LINE = (233, 237, 242)
BLUE = (59, 142, 234)
BTN_BORDER = (207, 214, 222)
OK = (46, 171, 91)
WARN = (230, 162, 60)
BAD = (229, 80, 74)
ORANGE = (194, 112, 58)


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


class Canvas:
    """极简绘图助手：管理 y 游标，减少手工算坐标出错。"""

    def __init__(self, width: int, height: int) -> None:
        self.img = Image.new("RGB", (width, height), BG)
        self.draw = ImageDraw.Draw(self.img)
        self.w = width

    def panel(self, x: int, y: int, w: int, h: int, fill=CARD, outline=None, radius=8):
        self.draw.rounded_rectangle([x, y, x + w, y + h], radius, fill=fill,
                                    outline=outline or fill)

    def text(self, x: int, y: int, content: str, size=12, bold=False, fill=INK):
        self.draw.text((x, y), content, font=font(size, bold), fill=fill)

    def right_text(self, right: int, y: int, content: str, size=12, bold=False, fill=INK):
        width = self.draw.textlength(content, font=font(size, bold))
        self.draw.text((right - width, y), content, font=font(size, bold), fill=fill)

    def stepper(self, x: int, y: int, label: str, desc: str, value: int,
                width: int = 430, testid: str = "") -> None:
        """一个 [-] N [+] 控件行。"""
        self.text(x, y + 2, label, 12.5)
        if desc:
            self.text(x + self.draw.textlength(label, font=font(12.5)) + 8, y + 4,
                      desc, 10.5, fill=MUTED)
        bx = x + width - 118
        for offset, symbol, enabled in ((0, "−", value > 0), (78, "+", value < 99)):
            fill = (255, 255, 255) if enabled else (247, 249, 251)
            color = (85, 95, 110) if enabled else (194, 202, 210)
            self.draw.rounded_rectangle([bx + offset, y, bx + offset + 26, y + 26],
                                        5, fill=fill, outline=BTN_BORDER)
            tw = self.draw.textlength(symbol, font=font(14))
            self.draw.text((bx + offset + 13 - tw / 2, y + 5), symbol,
                           font=font(14), fill=color)
        number = str(value)
        tw = self.draw.textlength(number, font=font(13.5, True))
        self.draw.text((bx + 52 - tw / 2, y + 5), number,
                       font=font(13.5, True), fill=INK if value else (194, 202, 210))


def build_demo(schema: dict) -> list[dict]:
    """与界面"载入示例"一致的 8 张订单。"""
    def blank(note):
        return {
            "note": note,
            "base": {g["id"]: 0 for g in schema["base_groups"]},
            "child_sub": {c["id"]: 0 for c in schema["child_sub_groups"]},
            "disability": {l["id"]: {b["id"]: 0 for b in schema["age_bands"]}
                           for l in schema["disability_levels"]},
            "pregnant": {s["id"]: {b["id"]: 0 for b in schema["age_bands"]}
                         for s in schema["pregnant_stages"]},
            "companion_count": None,
            "key_passenger_service": False,
            "services": [],
        }

    orders = []
    c = blank("正常一家")
    c["base"]["adult"] = 2
    c["base"]["child"] = 1
    c["child_sub"]["child_quiet"] = 1
    orders.append(c)

    c = blank("婴儿单独（应拒票）")
    c["base"]["infant"] = 1
    orders.append(c)

    c = blank("青少年独行（应通过）")
    c["base"]["youth"] = 1
    orders.append(c)

    c = blank("重度残疾·未约重点旅客（应拒票）")
    c["base"]["adult"] = 1
    c["base"]["child"] = 1
    c["disability"]["severe"]["child"] = 1
    orders.append(c)

    c = blank("重度残疾·已约重点旅客（应通过）")
    c["base"]["adult"] = 2
    c["base"]["child"] = 1
    c["disability"]["severe"]["child"] = 1
    c["key_passenger_service"] = True
    c["services"] = ["wheelchair"]
    orders.append(c)

    c = blank("照护人自身重度残疾（应拒票）")
    c["base"]["adult"] = 1
    c["base"]["child"] = 1
    c["disability"]["severe"]["adult"] = 1
    c["disability"]["severe"]["child"] = 1
    c["key_passenger_service"] = True
    orders.append(c)

    c = blank("37周以上孕妇（应拒票）")
    c["base"]["adult"] = 2
    c["pregnant"]["term"]["adult"] = 1
    orders.append(c)

    c = blank("全年龄段 + 独立维度")
    for key in ("youth", "child", "toddler", "infant"):
        c["base"][key] = 1
    c["base"]["adult"] = 2
    c["child_sub"]["child_quiet"] = 1
    c["disability"]["mild"]["adult"] = 1
    c["disability"]["severe"]["toddler"] = 1
    c["pregnant"]["mid"]["adult"] = 1
    c["key_passenger_service"] = True
    orders.append(c)
    return orders


def main() -> int:
    schema = get("/api/composition/schema")
    orders = build_demo(schema)
    checked = post("/api/composition/check", {"orders": orders})["orders"]
    # 展示第 1 张（正常一家）与第 5 张（重度残疾已约重点旅客）
    active = 4

    W = 1600
    canvas = Canvas(W, 2400)
    draw = canvas.draw

    # ---------------- 顶部标题 ----------------
    draw.rectangle([0, 0, W, 86], fill=BLUE)
    canvas.text(34, 14, "OrderEditor · 按人员构成组单", 23, True, (255, 255, 255))
    canvas.text(34, 50,
                "总人数只由基础分组求和；儿童细分 / 残疾 / 孕妇是独立维度，"
                "不计入总人数；陪同人已计入成人", 13, False, (226, 238, 252))

    y = 104
    # ---------------- OrderSwitcher（超出宽度就换行，别溢出页面） ----------------
    rows: list[list[tuple[str, int, bool]]] = [[]]
    used = 0
    max_w = W - 48 - 32
    for index, order in enumerate(orders):
        check = checked[index]
        # 用"!"而不是 ⚠：微软雅黑不含 U+26A0，Pillow 会画出方框（豆腐块）
        label = f"{order['note']}（{check['total_passengers']}人）" + (
            " !" if not check["ok"] else "")
        width = draw.textlength(label, font=font(12.5, index == active)) + 24
        if used + width + 7 > max_w and rows[-1]:
            rows.append([])
            used = 0
        rows[-1].append((label, index, not check["ok"]))
        used += width + 7
    switch_h = 32 + len(rows) * 30 + 6
    canvas.panel(24, y, W - 48, switch_h)
    canvas.text(40, y + 8, "OrderSwitcher", 11.5, True, MUTED)
    ry2 = y + 24
    for row in rows:
        bx = 40
        for label, index, bad in row:
            width = draw.textlength(label, font=font(12.5, index == active)) + 24
            if index == active:
                draw.rounded_rectangle([bx, ry2, bx + width, ry2 + 26], 6, fill=BLUE)
                canvas.text(bx + 12, ry2 + 6, label, 12.5, True, (255, 255, 255))
            else:
                draw.rounded_rectangle([bx, ry2, bx + width, ry2 + 26], 6,
                                       fill=(255, 255, 255),
                                       outline=BAD if bad else BTN_BORDER)
                canvas.text(bx + 12, ry2 + 6, label, 12.5, False,
                            BAD if bad else (85, 95, 110))
            bx += width + 7
        ry2 += 30
    draw.rounded_rectangle([40, ry2, 136, ry2 + 26], 6, fill=(255, 255, 255),
                           outline=BTN_BORDER)
    canvas.text(52, ry2 + 6, "+ 新建订单", 12.5, False, (85, 95, 110))
    y += switch_h + 14

    # ---------------- 左右分栏 ----------------
    # 左栏放基础分组 + 儿童细分 + 陪同服务；右栏放两个交叉矩阵。
    # 早期把"陪同与服务"塞在左栏、矩阵全在右栏，结果右栏 3 倍高于左栏、
    # 左栏下方大片空白。现在把陪同与服务移到右栏底部配平。
    left_w = 700
    right_x = 24 + left_w + 14
    right_w = W - 24 - right_x
    content = orders[active]
    check = checked[active]

    top = y
    # ===== 左栏：BaseGroup / ChildSubGroup / ServiceGroup =====
    ly = top
    canvas.panel(24, ly, left_w, 178)
    canvas.text(40, ly + 10, "基础分组", 13.5, True, (74, 90, 107))
    canvas.text(40 + draw.textlength("基础分组", font=font(13.5, True)) + 10, ly + 13,
                "总人数的唯一来源", 11, False, MUTED)
    draw.line([40, ly + 32, 24 + left_w - 16, ly + 32], fill=LINE)
    for index, group in enumerate(schema["base_groups"]):
        canvas.stepper(40, ly + 42 + index * 27, group["label"], group["desc"],
                       content["base"][group["id"]], left_w - 32,
                       testid=f"base-{group['id']}")
    ly += 178 + 12

    canvas.panel(24, ly, left_w, 118)
    canvas.text(40, ly + 10, "儿童细分", 13.5, True, (74, 90, 107))
    canvas.text(40 + draw.textlength("儿童细分", font=font(13.5, True)) + 10, ly + 13,
                "只是标签，不计入总人数", 11, False, MUTED)
    draw.line([40, ly + 32, 24 + left_w - 16, ly + 32], fill=LINE)
    for index, item in enumerate(schema["child_sub_groups"]):
        canvas.stepper(40, ly + 42 + index * 27, item["label"], "",
                       content["child_sub"][item["id"]], left_w - 32,
                       testid=f"childsub-{item['id']}")
    tagged = content["child_sub"]["child_quiet"] + content["child_sub"]["child_noisy"]
    canvas.text(40, ly + 96, f"儿童人数 {content['base']['child']}，已贴标签 {tagged}",
                11, False, MUTED)
    ly += 118 + 12

    # 左栏底部：分组规则速查（补平高度，同时把"哪些不计入总人数"摆明）
    rules = [
        ("总人数公式", "成人 + 青少年 + 儿童 + 幼儿 + 婴儿"),
        ("计入总人数的", "仅基础分组五档"),
        ("不计入总人数的", "儿童细分、残疾、孕妇、陪同人"),
        ("14 岁整", "属于青少年（儿童为「未满 14 周岁」）"),
        ("重度/极重度残疾", "强制重点旅客 + 每位至少 1 名成人陪同"),
        ("10 个月 / 37 周+", "强制重点旅客 + 至少 1 名成人陪同"),
        ("陪同人资格", "必须是成人；重/极重度残疾人不能充当陪同人"),
    ]
    rule_h = 40 + len(rules) * 21
    canvas.panel(24, ly, left_w, rule_h)
    canvas.text(40, ly + 10, "规则速查", 13.5, True, (74, 90, 107))
    draw.line([40, ly + 32, 24 + left_w - 16, ly + 32], fill=LINE)
    for index, (key, value) in enumerate(rules):
        ry3 = ly + 40 + index * 21
        canvas.text(40, ry3, key, 11.5, True, (85, 95, 110))
        canvas.text(40 + 150, ry3, value, 11.5, False, (110, 120, 132))
    ly += rule_h

    # ===== 右栏：DisabilityGroup / PregnantGroup / ServiceGroup =====
    ry = top
    dis_h = 92 + len(schema["disability_levels"]) * (20 + len(schema["age_bands"]) * 24)
    canvas.panel(right_x, ry, right_w, dis_h)
    canvas.text(right_x + 16, ry + 10, "残疾（独立维度）", 13.5, True, (74, 90, 107))
    canvas.text(right_x + 16 + draw.textlength("残疾（独立维度）", font=font(13.5, True)) + 10,
                ry + 13, "程度 × 年龄段，不计入总人数", 11, False, MUTED)
    draw.line([right_x + 16, ry + 32, right_x + right_w - 16, ry + 32], fill=LINE)
    cy = ry + 42
    for level in schema["disability_levels"]:
        flag = "  ← 强制重点旅客 + 成人陪同" if level["requires_key_service"] else ""
        canvas.text(right_x + 16, cy, level["label"], 12.5, True,
                    ORANGE if level["requires_key_service"] else (85, 95, 110))
        if flag:
            canvas.text(right_x + 16 + draw.textlength(level["label"], font=font(12.5, True)) + 4,
                        cy + 2, flag, 10.5, False, ORANGE)
        cy += 20
        for band in schema["age_bands"]:
            # 缩进矩阵
            draw.rectangle([right_x + 22, cy + 2, right_x + 24, cy + 22], fill=LINE)
            canvas.stepper(right_x + 32, cy, band["label"], "",
                           content["disability"][level["id"]][band["id"]],
                           right_w - 56, testid=f"dis-{level['id']}-{band['id']}")
            cy += 24
    ry += dis_h + 12

    preg_h = 92 + len(schema["pregnant_stages"]) * (20 + len(schema["age_bands"]) * 24)
    canvas.panel(right_x, ry, right_w, preg_h)
    canvas.text(right_x + 16, ry + 10, "孕妇（独立维度）", 13.5, True, (74, 90, 107))
    canvas.text(right_x + 16 + draw.textlength("孕妇（独立维度）", font=font(13.5, True)) + 10,
                ry + 13, "孕期 × 年龄段，不计入总人数", 11, False, MUTED)
    draw.line([right_x + 16, ry + 32, right_x + right_w - 16, ry + 32], fill=LINE)
    cy = ry + 42
    for stage in schema["pregnant_stages"]:
        if stage["requires_key_service"]:
            flag, color = "  ← 强制重点旅客 + 成人陪同", ORANGE
        elif stage.get("configurable"):
            flag, color = "  ← 平台可配置", MUTED
        else:
            flag, color = "", (85, 95, 110)
        canvas.text(right_x + 16, cy, stage["label"], 12.5, True, color)
        if flag:
            canvas.text(right_x + 16 + draw.textlength(stage["label"], font=font(12.5, True)) + 4,
                        cy + 2, flag, 10.5, False, color)
        cy += 20
        for band in schema["age_bands"]:
            draw.rectangle([right_x + 22, cy + 2, right_x + 24, cy + 22], fill=LINE)
            canvas.stepper(right_x + 32, cy, band["label"], "",
                           content["pregnant"][stage["id"]][band["id"]],
                           right_w - 56, testid=f"preg-{stage['id']}-{band['id']}")
            cy += 24
    ry += preg_h + 12

    # ===== 右栏底部：陪同与服务 =====
    canvas.panel(right_x, ry, right_w, 132)
    canvas.text(right_x + 16, ry + 10, "陪同与服务", 13.5, True, (74, 90, 107))
    canvas.text(right_x + 16 + draw.textlength("陪同与服务", font=font(13.5, True)) + 10,
                ry + 13, "陪同人已计入成人，不额外加人", 11, False, MUTED)
    draw.line([right_x + 16, ry + 32, right_x + right_w - 16, ry + 32], fill=LINE)
    companion = content["companion_count"] if content["companion_count"] is not None else 0
    canvas.stepper(right_x + 16, ry + 42, "陪同人数",
                   "可选登记；出票资格看可用健康成人",
                   companion, right_w - 32, testid="companion")
    canvas.text(right_x + 16, ry + 76, "重点旅客服务", 12.5)
    canvas.text(right_x + 16 + draw.textlength("重点旅客服务", font=font(12.5)) + 8, ry + 78,
                "重度/极重度残疾、37周以上孕妇强制要求", 10.5, False, MUTED)
    box_x = right_x + right_w - 16 - 118 - 26
    draw.rounded_rectangle([box_x, ry + 76, box_x + 15, ry + 91], 3,
                           fill=(255, 255, 255), outline=BTN_BORDER)
    if content["key_passenger_service"]:
        draw.line([box_x + 3, ry + 84, box_x + 6, ry + 87], fill=OK, width=2)
        draw.line([box_x + 6, ry + 87, box_x + 12, ry + 79], fill=OK, width=2)
    canvas.text(right_x + 16, ry + 100, "其它服务", 12.5)
    sx = right_x + 16 + draw.textlength("其它服务", font=font(12.5)) + 8
    for service, label in (("wheelchair", "轮椅"), ("stretcher", "担架"),
                           ("guide_dog", "导盲犬"), ("accessible_carriage", "无障碍车厢")):
        size = 15
        draw.rounded_rectangle([sx, ry + 99, sx + size, ry + 99 + size], 3,
                               fill=(255, 255, 255), outline=BTN_BORDER)
        if service in content.get("services", []):
            # 对勾必须画在框内且留边距：早期坐标越界，视觉上像 ☑ 字形
            draw.line([sx + 3, ry + 107, sx + 6, ry + 110], fill=OK, width=2)
            draw.line([sx + 6, ry + 110, sx + 12, ry + 102], fill=OK, width=2)
        canvas.text(sx + size + 5, ry + 100, label, 11, False, (85, 95, 110))
        sx += size + 5 + draw.textlength(label, font=font(11)) + 16
    ry += 132

    y = max(ly, ry) + 14

    # ---------------- OrderSummary ----------------
    canvas.panel(24, y, W - 48, 132)
    canvas.text(40, y + 10, "OrderSummary", 11.5, True, MUTED)
    base = content["base"]
    total = sum(base.values())
    healthy = check["healthy_adults"]
    need = check["required_companions"]
    items = [
        ("总人数", str(total), BLUE),
        ("成人", str(base["adult"]), INK),
        ("青少年", str(base["youth"]), INK),
        ("儿童", str(base["child"]), INK),
        ("幼儿", str(base["toddler"]), INK),
        ("婴儿", str(base["infant"]), INK),
        ("可用健康成人", str(healthy), INK),
        ("所需陪同", str(need), INK),
    ]
    # 等宽分栏，避免列间距大于字宽时互相挤压
    cell = (W - 48 - 32) / len(items)
    for index, (label, value, color) in enumerate(items):
        cx = 40 + index * cell
        canvas.text(cx, y + 32, label, 11.5, False, MUTED)
        canvas.text(cx, y + 50, value, 21, True, color)
    canvas.text(40, y + 82,
                "总人数 = 成人 + 青少年 + 儿童 + 幼儿 + 婴儿 = "
                f"{base['adult']} + {base['youth']} + {base['child']} + "
                f"{base['toddler']} + {base['infant']} = {total}"
                "　｜　儿童细分 / 残疾 / 孕妇 均不计入总人数", 11.5, False, MUTED)
    if check["ok"]:
        draw.rounded_rectangle([40, y + 104, W - 40, y + 124], 5, fill=(240, 251, 244))
        draw.rectangle([40, y + 104, 43, y + 124], fill=OK)
        canvas.text(52, y + 107, "校验通过 —— 可以出票", 12, False, (33, 122, 69))
    else:
        draw.rounded_rectangle([40, y + 104, W - 40, y + 124], 5, fill=(253, 242, 241))
        draw.rectangle([40, y + 104, 43, y + 124], fill=BAD)
        canvas.text(52, y + 107, "不予出票：" + "；".join(check["errors"]), 12, False, (179, 53, 47))
    y += 132 + 14

    # ---------------- OrderActions ----------------
    canvas.panel(24, y, W - 48, 56)
    canvas.text(40, y + 8, "OrderActions", 11.5, True, MUTED)
    ax = 40
    for label, primary in (("提交全部订单", True), ("+ 新建订单", False),
                           ("复制到新订单", False), ("载入示例（含拒票单）", False),
                           ("清空", False)):
        width = draw.textlength(label, font=font(12.5, primary)) + 28
        draw.rounded_rectangle([ax, y + 24, ax + width, y + 50], 6,
                               fill=BLUE if primary else (255, 255, 255),
                               outline=BLUE if primary else BTN_BORDER)
        canvas.text(ax + 14, y + 30, label, 12.5, primary,
                    (255, 255, 255) if primary else (85, 95, 110))
        ax += width + 8
    y += 56 + 18

    img = canvas.img.crop((0, 0, W, min(y, 2400)))
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB  {img.size}")
    print(f"展示订单：{content['note']}｜总人数 {total}｜"
          f"可用健康成人 {healthy}｜所需陪同 {need}｜"
          f"校验 {'通过' if check['ok'] else '不通过'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制 OrderEditor 组件布局图")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help=f"输出 PNG 路径（默认 {DEFAULT_OUT.relative_to(ROOT)}）")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
