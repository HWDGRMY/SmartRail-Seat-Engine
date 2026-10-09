"""画"按顺序占座 vs 打散占座"的余票格局差异（真实数据）。

用法::

    python tools/draw_fragmentation.py

这张图回答一个问题：**为什么压测不能按顺序占座位。**
按顺序占时余票永远是车厢末尾一整块，任何订单都坐得下，
"凑不出连座 -> 自动分票"这个场景永远触发不到 —— 等于把要测的东西测没了。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "docs" / "screenshots" / "fragmentation.png"
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

BG = (242, 244, 247)
CARD = (255, 255, 255)
INK = (51, 51, 51)
MUTED = (123, 135, 148)
FREE = (214, 232, 250)
SOLD = (150, 165, 182)
GOOD = (46, 171, 91)
BAD = (229, 80, 74)
BLUE = (59, 142, 234)

TARGET_REMAINING = 100


def font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def layout(store, class_code: str = "二等座"):
    """返回 {车厢: [排 -> [列: 是否占用]]}，只取有数据的部分。"""
    rows: dict[int, dict[int, dict[str, bool]]] = {}
    for seat in store.formation.seats:
        if seat.class_code != class_code:
            continue
        occupied = seat.seat_id in store.occupied
        rows.setdefault(seat.carriage, {}).setdefault(seat.row, {})[seat.col] = occupied
    return rows


def longest_run(store, class_code: str = "二等座") -> int:
    """最长连续空座。

    **直接复用 ``DevStore.fragmentation()``**，不在这里另算一遍 ——
    早期本文件自己实现了相邻判断，漏了"按物理列位判断"这一步，
    于是把隔着过道的组合也算成连座（最长连座报成 5，实际是 2）。
    同一份事实在两处各算一遍，迟早会分叉。
    """
    return store.fragmentation(class_code)["longest_run"]


def draw_panel(canvas, x, y, w, title, subtitle, rows, accent):
    draw = canvas.draw
    head_h = 62
    draw.rounded_rectangle([x, y, x + w, y + head_h], 9, fill=accent)
    canvas.text(x + 18, y + 10, title, 17, True, (255, 255, 255))
    canvas.text(x + 18, y + 36, subtitle, 11.5, False, (235, 245, 245))

    # 只画"确实还有自由座位"的车厢。按车厢号取前 6 个会让顺序占那张图
    # 全是已售车厢（自由座位都在末尾几节），什么也说明不了。
    interesting = [
        c for c in sorted(rows)
        if any(not occ for r in rows[c].values() for occ in r.values())
    ][:6]
    if not interesting:
        interesting = sorted(rows)[:6]

    cell_w, cell_h = 17, 15
    body_y = y + head_h + 14
    carriage_x = x + 18
    for carriage in interesting:
        free_here = sum(
            1 for r in rows[carriage].values() for occ in r.values() if not occ
        )
        canvas.text(carriage_x, body_y, f"{carriage:02d} 车", 12, True, INK)
        canvas.text(carriage_x + 44, body_y + 1, f"{free_here} 空", 10.5, False, MUTED)
        ry = body_y + 22
        for row in sorted(rows[carriage]):
            seats = rows[carriage][row]
            cx = carriage_x
            for col in "ABCDF":
                if col not in seats:
                    cx += cell_w
                    continue
                occupied = seats[col]
                draw.rounded_rectangle(
                    [cx, ry, cx + cell_w - 3, ry + cell_h - 3], 3,
                    fill=SOLD if occupied else FREE,
                    outline=(120, 135, 152) if occupied else BLUE,
                )
                cx += cell_w
            ry += cell_h
        carriage_x += 5 * cell_w + 34
    return body_y + 22 + 9 * cell_h + 10


def main() -> int:
    from smartrail.ticketing import reset_dev_store

    sequential = reset_dev_store()
    sequential.set_remaining("二等座", TARGET_REMAINING, from_front=True)
    scattered = reset_dev_store()
    scattered.set_remaining("二等座", TARGET_REMAINING)

    seq_rows = layout(sequential)
    sca_rows = layout(scattered)
    seq_frag = sequential.fragmentation("二等座")
    sca_frag = scattered.fragmentation("二等座")
    seq_run = seq_frag["longest_run"]
    sca_run = sca_frag["longest_run"]

    W = 1500
    img = Image.new("RGB", (W, 1180), BG)
    draw = ImageDraw.Draw(img)

    class _Canvas:
        """极简画布：只提供这个脚本用到的两个方法。"""

        def __init__(self, draw: ImageDraw.ImageDraw) -> None:
            self.draw = draw

        def text(self, x, y, content, size=12, bold=False, fill=INK):
            draw.text((x, y), content, font=font(size, bold), fill=fill)

    canvas = _Canvas(draw)

    draw.rectangle([0, 0, W, 84], fill=(43, 58, 77))
    canvas.text(32, 14, "压测为什么不能按顺序占座位", 23, True, (255, 255, 255))
    canvas.text(32, 50,
                f"两者余票数量完全相同（都是 {TARGET_REMAINING} 张），"
                "但能否凑出连座完全不同", 13, False, (190, 205, 220))

    y = 100
    panel_w = (W - 44 - 20) // 2
    draw.rounded_rectangle([22, y, 22 + panel_w, y + 44], 9, fill=CARD)
    canvas.text(40, y + 12, "① 按顺序占（旧默认 from_front=True）", 13, True, BAD)
    canvas.text(40 + panel_w - 300, y + 14,
                f"最长连座 {seq_run} · 自由座位 {seq_frag['rows_with_free_seats']} 排",
                12, True, BAD)
    end_left = draw_panel(
        canvas, 22, y + 52, panel_w, "余票 = 车厢末尾一整块连续座位",
        "任何订单都坐得下 —— 分票场景永远触发不到", seq_rows, (150, 90, 88))

    draw.rounded_rectangle([22 + panel_w + 20, y, W - 22, y + 44], 9, fill=CARD)
    canvas.text(42 + panel_w, y + 12, "② 打散占（现默认，模拟真实售票）", 13, True, GOOD)
    canvas.text(42 + panel_w + panel_w - 300, y + 14,
                f"最长连座 {sca_run} · 自由座位 {sca_frag['rows_with_free_seats']} 排",
                12, True, GOOD)
    draw_panel(
        canvas, 22 + panel_w + 20, y + 52, panel_w, "余票碎片化散布在各车厢",
        "凑不出连座 -> 自动分票被真实触发", sca_rows, (60, 130, 95))

    y = end_left + 22
    draw.rounded_rectangle([22, y, W - 22, y + 128], 9, fill=CARD)
    canvas.text(40, y + 12, "两种占位方式的可测性对比", 15, True, INK)
    headers = ["指标", "① 按顺序占", "② 打散占", "为什么重要"]
    widths = [300, 190, 190, 520]
    cx = 40
    for head, width in zip(headers, widths):
        canvas.text(cx, y + 42, head, 11.5, True, MUTED)
        cx += width
    values = [
        ("自由座位所在排数", str(seq_frag["rows_with_free_seats"]),
         str(sca_frag["rows_with_free_seats"]), "越多越接近真实售票"),
        ("最长连续空座", str(seq_run), str(sca_run),
         "决定「N 人同行能否连座」"),
        ("≥2 连座处数", str(seq_frag["runs_at_least_2"]),
         str(sca_frag["runs_at_least_2"]), "决定 2 人单是否要分票"),
        ("≥3 连座处数", str(seq_frag["runs_at_least_3"]),
         str(sca_frag["runs_at_least_3"]), "决定带娃家庭是否被拆分"),
    ]
    ry = y + 64
    for label, left, right, why in values:
        cx = 40
        for text_value, width, color, bold in (
            (label, widths[0], INK, False),
            (left, widths[1], BAD, True),
            (right, widths[2], GOOD, True),
            (why, widths[3], MUTED, False),
        ):
            canvas.text(cx, ry, text_value, 12, bold, color)
            cx += width
        ry += 15

    y += 128 + 18
    draw.rounded_rectangle([22, y, W - 22, y + 92], 9, fill=(253, 248, 240))
    canvas.text(40, y + 14, "结论", 14, True, (168, 112, 32))
    canvas.text(40, y + 40,
                "按顺序占座 → 余票恒为一大块连续座位 → 「余票不足需分票」永远测不到，"
                "这是把要测的东西测没了，不是少测一个边界。",
                12.5, False, INK)
    canvas.text(40, y + 62,
                "现在默认打散（含 30% 相邻对，模拟同行旅客买连座），"
                "并可复现（固定随机种子）。", 12, False, MUTED)

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 裁掉底部空白：画布高度是预留的，实际内容到结论框为止
    cropped = img.crop((0, 0, W, y + 92 + 22))
    cropped.save(target)
    print(f"PNG: {target}  {target.stat().st_size / 1024:.0f} KB  {cropped.size}")
    print(f"顺序占：最长连座 {seq_run}，自由座位 {seq_frag['rows_with_free_seats']} 排")
    print(f"打散占：最长连座 {sca_run}，自由座位 {sca_frag['rows_with_free_seats']} 排")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="绘制余票碎片化对比图")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(main())
