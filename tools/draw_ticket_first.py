"""绘制"出票优先策略"场景对比图（真实求解数据，不依赖浏览器）。

用法::

    python tools/draw_ticket_first.py                   # 输出到 docs/screenshots/
    python tools/draw_ticket_first.py --out 我的图.png

为什么不用无头浏览器截图：部分受限环境不允许浏览器启动 crashpad/mojo 子进程
（`OpenProcess: 拒绝访问 (0x5)`），headless 截图不可用。因此改为用 Pillow
按**真实求解结果**重绘同样的布局 —— 数字与座位都是真的，只是渲染器不同。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smartrail.report import _ticket_first_scenarios, ticket_first_layout  # noqa: E402

DEFAULT_OUT = ROOT / "docs" / "screenshots" / "ticket-first.png"
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

BG = (242, 244, 247)
CARD = (255, 255, 255)
INK = (51, 51, 51)
MUTED = (123, 135, 148)
BLUE = (59, 142, 234)
OK = (46, 171, 91)
WARN = (230, 162, 60)
BAD = (229, 80, 74)
PAX_FILL = (59, 142, 234)

LEVEL_COLOR = {"satisfied": OK, "compromised": WARN, "impossible": BAD}
LEVEL_TEXT = {
    "satisfied": "相邻已满足",
    "compromised": "未相邻（本车厢仍有相邻空位）",
    "impossible": "本车厢已无相邻空位",
}


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def main() -> int:
    scenarios = _ticket_first_scenarios()
    # 座位布局由顶层共享（场景只存占座差异），与页面口径一致
    layout_seats = ticket_first_layout()["seats"]
    W = 1560
    IMG_H = 2000
    img = Image.new("RGB", (W, IMG_H), BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, W, 86], fill=BLUE)
    draw.text((32, 14), "出票优先策略 · 没有相邻座位时怎么办", font=font(23, True), fill=(255, 255, 255))
    draw.text(
        (32, 50),
        "原则：能出票就出票；把需要现场处理的事明确交办出去。拒票只留给『确实一个空座都没有』。",
        font=font(13), fill=(226, 238, 252),
    )

    y = 106
    row_h = 176
    for scenario in scenarios:
        panel_h = row_h
        draw.rounded_rectangle([24, y, W - 24, y + panel_h], 10, fill=CARD)
        # 标题
        draw.text((40, y + 12), scenario["title"], font=font(15, True), fill=INK)
        level = scenario["adjacency_level"]
        lx = 40 + draw.textlength(scenario["title"], font=font(15, True)) + 14
        draw.text((lx, y + 14), LEVEL_TEXT.get(level, level), font=font(12.5),
                  fill=LEVEL_COLOR.get(level, MUTED))
        # 指标
        metrics = [
            ("出票 / 请求", f"{len(scenario['assignments'])} / "
             f"{len(scenario['assignments']) + len(scenario['waitlisted'])}",
             BAD if scenario["waitlisted"] else OK),
            ("拒票", str(len(scenario["waitlisted"])), BAD if scenario["waitlisted"] else OK),
            ("Tier 0", str(scenario["tier0"]), BAD if scenario["tier0"] else OK),
            ("车厢占用", f"{scenario['occupancy'] * 100:.0f}%", INK),
            ("本车厢还有相邻空位", "有" if scenario["adjacent_pair_available"] else "无",
             OK if scenario["adjacent_pair_available"] else BAD),
            ("代价", f"{scenario['total_cost']:.0f}", OK if scenario["total_cost"] <= 0 else BAD),
        ]
        mx = 40
        for label, value, color in metrics:
            draw.text((mx, y + 38), label, font=font(11), fill=MUTED)
            draw.text((mx, y + 54), value, font=font(15, True), fill=color)
            mx += 188

        # 座位小图（只画涉及的车厢首两排）
        by_seat = {a["seat_id"]: a["passenger_id"] for a in scenario["assignments"]}
        occupied = set(scenario.get("occupied_seats", []))
        used_cars = sorted({a["carriage"] for a in scenario["assignments"]}) or [1]
        sx = 40
        sy = y + 84
        for car_no in used_cars[:2]:
            car_seats = [s for s in layout_seats if s["c"] == car_no]
            rows = sorted({s["r"] for s in car_seats})[:2]
            draw.text((sx, sy), f"{car_no:02d} 车", font=font(11.5, True), fill=MUTED)
            cy = sy + 18
            for row in rows:
                cx = sx
                draw.text((cx, cy + 4), str(row), font=font(10), fill=(168, 176, 186))
                cx += 20
                for seat in sorted([s for s in car_seats if s["r"] == row], key=lambda s: s["o"]):
                    box = [cx, cy, cx + 26, cy + 24]
                    pid = by_seat.get(seat["i"])
                    if pid:
                        draw.rounded_rectangle(box, 4, fill=PAX_FILL)
                        label = pid.split("-")[0][:3]
                        tw = draw.textlength(label, font=font(10, True))
                        draw.text((cx + (26 - tw) / 2, cy + 6), label,
                                  font=font(10, True), fill=(255, 255, 255))
                    elif seat["i"] in occupied:
                        draw.rounded_rectangle(box, 4, fill=(233, 237, 242))
                    else:
                        outline = (207, 214, 222)
                        fill = (255, 255, 255)
                        if seat.get("q"):
                            outline, fill = (183, 169, 245), (247, 245, 255)
                        elif seat.get("a"):
                            outline, fill = (127, 211, 216), (240, 251, 252)
                        draw.rounded_rectangle(box, 4, fill=fill, outline=outline)
                        # 注意：共享布局里的 ``o`` 是**列序号（int）**，不是列字母。
                        # 直接把它喂给 draw.textlength 会报
                        # "argument of type 'int' is not iterable"。
                        label = "ABCDEF"[seat["o"]] if 0 <= seat["o"] < 6 else "?"
                        tw = draw.textlength(label, font=font(10))
                        draw.text((cx + (26 - tw) / 2, cy + 6), label,
                                  font=font(10), fill=(125, 135, 148))
                    cx += 29
                cy += 27
            sx += 230

        # 提示
        tx = 40 + 470
        ty = y + 84
        draw.text((tx, ty - 16), "待办提示", font=font(11.5, True), fill=MUTED)
        if not scenario["notices"]:
            draw.text((tx, ty), "无需现场处理的提示（相邻已满足）", font=font(12), fill=OK)
        for notice in scenario["notices"][:3]:
            color = BAD if notice["level"] == "action" else WARN
            draw.text((tx, ty), f"● {notice['kind']}", font=font(12, True), fill=color)
            ty += 19
            message = notice["message"]
            # 手工折行，避免超出卡片
            limit = 74
            while message:
                draw.text((tx + 12, ty), message[:limit], font=font(11.5), fill=(85, 95, 110))
                message = message[limit:]
                ty += 17
            ty += 4
        y += panel_h + 12

    total_h = y + 16
    img = img.crop((0, 0, W, min(total_h, IMG_H)))
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB  {img.size}")
    print(f"场景数: {len(scenarios)}，其中无相邻空位的: "
          f"{sum(1 for s in scenarios if not s['adjacent_pair_available'])}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制出票优先策略场景对比图")
    parser.add_argument(
        "--out", default=str(DEFAULT_OUT),
        help=f"输出 PNG 路径（默认 {DEFAULT_OUT.relative_to(ROOT)}）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
