"""把 OrderEditor（人员构成组单）的真实校验与出票结果画成图。

用法::

    python tools/draw_composer.py
    python tools/draw_composer.py --out 我的图.png

调用 ``/api/composition/check`` 与 ``/api/composition/submit`` 取真实数据，
用 Pillow 重绘面板布局（受限环境无法无头截图，见 tools/README.md）。
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

DEFAULT_OUT = ROOT / "docs" / "screenshots" / "order-editor.png"
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
PANEL = (251, 252, 253)


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


def build_demo(schema: dict) -> list[dict]:
    """构造覆盖全部规则的示例订单。"""
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
    c = blank("成人2 + 儿童1（安静）")
    c["base"]["adult"] = 2
    c["base"]["child"] = 1
    c["child_sub"]["child_quiet"] = 1
    orders.append(c)

    c = blank("婴儿单独（应拒票）")
    c["base"]["infant"] = 1
    orders.append(c)

    c = blank("青少年单独（应通过）")
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

    c = blank("全年龄段 + 独立维度（总人数 = 基础之和）")
    for key in ("adult", "youth", "child", "toddler", "infant"):
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
    checked = post("/api/composition/check", {"orders": orders})
    submitted = post("/api/composition/submit", {"orders": orders})

    W = 1600
    IMG_H = 2400
    img = Image.new("RGB", (W, IMG_H), BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, W, 88], fill=BLUE)
    draw.text((34, 14), "按人员构成组单 · 真实数据", font=font(24, True), fill=(255, 255, 255))
    draw.text(
        (34, 52),
        "总人数只由基础分组求和；儿童细分 / 残疾 / 孕妇是独立维度，不计入总人数",
        font=font(13), fill=(226, 238, 252),
    )

    y = 108

    # ---- 基础分组与独立维度定义 ----
    def_h = 168
    draw.rounded_rectangle([24, y, W - 24, y + def_h], 10, fill=CARD)
    draw.text((40, y + 13), "分组定义", font=font(15, True), fill=INK)
    col = 40
    draw.text((col, y + 42), "基础分组（总人数唯一来源）", font=font(12.5, True), fill=(74, 90, 107))
    for index, group in enumerate(schema["base_groups"]):
        draw.text((col, y + 64 + index * 17), f"{group['label']}  {group['desc']}",
                  font=font(12), fill=(85, 95, 110))
    col = 520
    draw.text((col, y + 42), "残疾（程度 × 年龄段）", font=font(12.5, True), fill=(74, 90, 107))
    for index, level in enumerate(schema["disability_levels"]):
        flag = "  ← 强制重点旅客+陪同" if level["requires_key_service"] else ""
        draw.text((col, y + 64 + index * 17), f"{level['label']}{flag}",
                  font=font(12), fill=(194, 112, 58) if level["requires_key_service"] else (85, 95, 110))
    col = 900
    draw.text((col, y + 42), "孕妇（孕期 × 年龄段）", font=font(12.5, True), fill=(74, 90, 107))
    for index, stage in enumerate(schema["pregnant_stages"]):
        flag = "  ← 强制重点旅客+陪同" if stage["requires_key_service"] else (
            "  ← 平台可配置" if stage.get("configurable") else "")
        draw.text((col, y + 64 + index * 17), f"{stage['label']}{flag}",
                  font=font(12), fill=(194, 112, 58) if stage["requires_key_service"] else (85, 95, 110))
    # 公式单独一行，放在列表下方留出足够间距（列表 5 行到 y+64+4*17+16）
    draw.text((40, y + def_h - 22),
              f"总人数公式：{schema['total_formula']}"
              f"　｜　不计入总人数：{'、'.join(schema['excluded_from_total'])}",
              font=font(12), fill=MUTED)
    y += def_h + 16

    # ---- 逐单校验结论 ----
    # 行高要能容纳"标题 + 基础分组 + 独立维度 + 拒票原因"四行，
    # 否则拒票原因会压到下一张订单的标题上（第一版行高 58 就撞了）。
    row_h = 76
    panel_h = 42 + len(submitted["orders"]) * row_h
    draw.rounded_rectangle([24, y, W - 24, y + panel_h], 10, fill=CARD)
    draw.text((40, y + 13),
              f"逐单校验与出票（{len(submitted['orders'])} 张订单，"
              f"拦下 {submitted['summary']['blocked_orders']} 张）",
              font=font(15, True), fill=INK)
    oy = y + 42
    for index, item in enumerate(submitted["orders"]):
        blocked = item["blocked"]
        color = BAD if blocked else OK
        draw.rectangle([40, oy, 44, oy + row_h - 12], fill=color)
        note = item.get("note") or item["order_id"]
        draw.text((56, oy), note[:34], font=font(13, True), fill=INK)
        # 基础分组摘要
        base = item["info"]["base"]
        summary = " ".join(f"{k}{v}" for k, v in base.items() if v)
        draw.text((56, oy + 20), f"基础分组：{summary or '（空）'}", font=font(11.5), fill=(85, 95, 110))
        # 独立维度摘要
        dims = []
        for level, row in item["info"]["disability"].items():
            total = sum(row.values())
            if total:
                dims.append(f"残疾·{level} {total}")
        for stage, row in item["info"]["pregnant"].items():
            total = sum(row.values())
            if total:
                dims.append(f"孕妇·{stage} {total}")
        if item["info"]["child_sub"]["child_quiet"]:
            dims.append(f"安静 {item['info']['child_sub']['child_quiet']}")
        if item["info"]["child_sub"]["child_noisy"]:
            dims.append(f"吵闹 {item['info']['child_sub']['child_noisy']}")
        draw.text((56, oy + 37), "独立维度：" + ("、".join(dims) or "无"),
                  font=font(11), fill=MUTED)
        # 右侧：总人数与结论
        right = (f"总人数 {item['total_passengers']}　"
                 f"可用健康成人 {item['check']['healthy_adults']}　"
                 f"所需陪同 {item['check']['required_companions']}")
        tw = draw.textlength(right, font=font(11.5))
        draw.text((W - 40 - tw, oy + 2), right, font=font(11.5), fill=INK)
        verdict = "不予出票" if blocked else f"出票 {len(item['seats'])}/{item['total_passengers']}"
        tw = draw.textlength(verdict, font=font(12.5, True))
        draw.text((W - 40 - tw, oy + 22), verdict, font=font(12.5, True), fill=color)
        if blocked and item["check"]["errors"]:
            draw.text((56, oy + 55), "· " + item["check"]["errors"][0][:78],
                      font=font(11), fill=BAD)
        elif not blocked and item["info"]["key_passenger_service"]:
            draw.text((56, oy + 55), "· 已预约重点旅客服务，站车协助",
                      font=font(11), fill=(194, 112, 58))
        oy += row_h
    y += panel_h + 16

    # ---- 汇总 ----
    draw.rounded_rectangle([24, y, W - 24, y + 66], 10, fill=CARD)
    s = submitted["summary"]
    metrics = [
        ("订单", str(s["orders"])),
        ("不予出票", str(s["blocked_orders"])),
        ("实际提交", str(s["submitted_orders"])),
        ("出票人数", str(s["seated_passengers"])),
        ("候补", str(s["waitlisted_passengers"])),
        ("Tier 0", str(s["tier0_violations"])),
        ("总耗时", f"{s['wall_ms']} ms"),
    ]
    box_w = (W - 48 - 32) / len(metrics)
    for index, (label, value) in enumerate(metrics):
        x = 40 + index * box_w
        draw.text((x, y + 12), label, font=font(11.5), fill=MUTED)
        draw.text((x, y + 30), value, font=font(19, True), fill=INK)
    y += 66 + 20

    img = img.crop((0, 0, W, min(y, IMG_H)))
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB  {img.size}")
    print(f"订单 {s['orders']}，拦下 {s['blocked_orders']}，"
          f"提交 {s['submitted_orders']}，出票 {s['seated_passengers']} 人，"
          f"Tier0 {s['tier0_violations']}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制 OrderEditor 校验与出票结果图")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help=f"输出 PNG 路径（默认 {DEFAULT_OUT.relative_to(ROOT)}）")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
