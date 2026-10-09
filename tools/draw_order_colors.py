"""画一张"按订单着色"的座位图，用来肉眼核对颜色是否真的区分开了。

严格照抄 developer.html 里的 seatClass / orderColorIndex / ORDER_PALETTE，
所以图上看到什么，页面上就是什么。
"""

import json
import sys
import urllib.request

sys.path.insert(0, r"F:\PycharmProjects\SmartRail-Seat-Engine")

OUT = r"F:\PycharmProjects\SmartRail-Seat-Engine\docs\screenshots\order-colors.png"
B = "http://127.0.0.1:8000"

ORDER_PALETTE = [
    {"bg": "#a8c4e8", "bd": "#7fa4d4", "fg": "#123a6b"},
    {"bg": "#f2c9a0", "bd": "#dda86f", "fg": "#6b3d10"},
    {"bg": "#a9d9b8", "bd": "#7cbf90", "fg": "#0f4a24"},
    {"bg": "#e3b6d6", "bd": "#c98bb8", "fg": "#5d1f4c"},
    {"bg": "#c9c2ea", "bd": "#a49ad6", "fg": "#2f2266"},
    {"bg": "#f0d79a", "bd": "#d9b862", "fg": "#5c4410"},
    {"bg": "#a5d6d6", "bd": "#76bcbc", "fg": "#0d4444"},
    {"bg": "#eab8b3", "bd": "#d08b84", "fg": "#6b2119"},
    {"bg": "#bcd3a4", "bd": "#93b374", "fg": "#2f4413"},
    {"bg": "#d9c3a5", "bd": "#bfa077", "fg": "#4f3a1c"},
]
_order_color_map: dict[str, int] = {}
_order_seq = 0


def order_color_index(order_id: str, fallback: int = 0) -> int:
    global _order_seq
    if not order_id:
        return fallback % len(ORDER_PALETTE)
    if order_id not in _order_color_map:
        _order_color_map[order_id] = _order_seq
        _order_seq += 1
    return _order_color_map[order_id] % len(ORDER_PALETTE)


def seat_class(seat: dict) -> str:
    if not seat.get("occupied"):
        return "bayslot" if seat.get("bay_slot") else ""
    if seat.get("source") == "manual":
        return "manual"
    if seat.get("source") == "preset":
        return "preset"
    return "orderc"


def get(path):
    with urllib.request.urlopen(B + path, timeout=60) as r:
        return json.loads(r.read().decode())


def post(path, body):
    request = urllib.request.Request(
        B + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as r:
        return json.loads(r.read().decode())


def main() -> int:
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int, bold: bool = False):
        for name in (("msyhbd.ttc" if bold else "msyh.ttc"),
                     ("simhei.ttf" if bold else "simhei.ttf")):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    schema = get("/api/composition/schema")
    bands = [b["id"] for b in schema["age_bands"]]

    post("/api/dev/reset", {"passengers": True})
    post("/api/dev/remaining", {"class_code": "二等座", "remaining": 260})

    # 造 4 张单，看颜色是否互相区分
    plans = [
        {"adult": 2, "infant": 2},
        {"adult": 3, "infant": 0},
        {"adult": 2, "infant": 1},
        {"adult": 4, "infant": 0},
    ]
    for plan in plans:
        post("/api/composition/submit", {"orders": [{
            "class_code": "二等座",
            "base": {"adult": plan["adult"], "child": 0, "youth": 0,
                     "toddler": 0, "infant": plan["infant"]},
            "child_sub": {g["id"]: 0 for g in schema["child_sub_groups"]},
            "disability": {lv["id"]: {b: 0 for b in bands}
                           for lv in schema["disability_levels"]},
            "pregnant": {st["id"]: {b: 0 for b in bands}
                         for st in schema["pregnant_stages"]},
        }]})

    snap = get("/api/dev/snapshot")
    orders = [o for o in snap["orders"] if not o["blocked"]]
    # 让新订单优先拿色，与页面一致
    for order in reversed(orders):
        order_color_index(order["order_id"])

    # 画座位图
    cars = snap["carriages"]
    show = [c for c in cars if c["number"] in (4, 6, 9, 10, 12, 14)]
    cell_w, cell_h, gap = 30, 25, 3
    rno_w = 30
    car_w = rno_w + 5 * (cell_w + gap) + 24
    car_h = 40 + 6 * (cell_h + gap) + 20
    cols = 3
    rows = (len(show) + cols - 1) // cols
    W = 40 + cols * (car_w + 18)
    H = 150 + rows * (car_h + 18)
    image = Image.new("RGB", (W, H), (244, 247, 250))
    canvas = ImageDraw.Draw(image)

    canvas.text((20, 16), "座位图按订单着色", font=font(19, True),
                fill=(30, 41, 59))
    canvas.text((20, 46), "同一订单一个颜色，不同订单不同颜色；"
                          "并标注每单的行数（是否坐在一起）",
                font=font(12), fill=(110, 122, 138))

    # 图例
    lx = 20
    for order in orders:
        palette = ORDER_PALETTE[order_color_index(order["order_id"])]
        seats = [p["seat_id"] for p in order["passengers"] if p.get("seat_id")]
        rows_used = sorted({s[:2] + "车" + s[3:5] + "排" for s in seats})
        label = (f"{order['order_id']}  {len(seats)} 座  "
                 f"{len(rows_used)} 排")
        tw = int(canvas.textlength(label, font=font(12)))
        canvas.rounded_rectangle([lx, 74, lx + 15, 89], 3,
                                 fill=palette["bg"], outline=palette["bd"])
        canvas.text((lx + 21, 76), label, font=font(12), fill=(60, 72, 88))
        lx += 21 + tw + 18

    by_car: dict[int, list[dict]] = {}
    for seat in snap["seats"]:
        by_car.setdefault(seat["carriage"], []).append(seat)

    for index, car in enumerate(show):
        cx = 20 + (index % cols) * (car_w + 18)
        cy = 112 + (index // cols) * (car_h + 18)
        canvas.rounded_rectangle([cx, cy, cx + car_w, cy + car_h], 9,
                                 fill=(255, 255, 255), outline=(222, 228, 235))
        canvas.text((cx + 12, cy + 9),
                    f"{car['number']:02d}车", font=font(13, True),
                    fill=(37, 51, 71))
        canvas.text((cx + 62, cy + 11), f"余 {car['remaining']}",
                    font=font(11), fill=(120, 132, 148))
        seats = {s["col"]: s for s in by_car.get(car["number"], [])
                 if s["row"] <= 6}
        cols_list = car["columns"]
        for r in range(1, 7):
            ry = cy + 34 + (r - 1) * (cell_h + gap)
            canvas.text((cx + 12, ry + 6), str(r), font=font(10),
                        fill=(150, 160, 172))
            for ci, col in enumerate(cols_list):
                sxx = cx + rno_w + ci * (cell_w + gap)
                seat = next((s for s in by_car.get(car["number"], [])
                             if s["row"] == r and s["col"] == col), None)
                if seat is None:
                    continue
                cls = seat_class(seat)
                if cls == "orderc":
                    palette = ORDER_PALETTE[
                        order_color_index(seat["order_id"],
                                          seat.get("color_index") or 0)]
                    fill, outline, fg = (palette["bg"], palette["bd"],
                                         palette["fg"])
                elif cls == "manual":
                    fill, outline, fg = "#d9c7a8", "#c8b394", "#6b5a3d"
                elif cls == "preset":
                    fill, outline, fg = "#c9d4e0", "#b7c4d3", "#5a6a7c"
                elif cls == "bayslot":
                    fill, outline, fg = "#e6f7f7", "#a9dede", "#0f7d7d"
                else:
                    fill, outline, fg = "#ffffff", "#cfd6de", "#98a2ae"
                canvas.rounded_rectangle(
                    [sxx, ry, sxx + cell_w, ry + cell_h], 3,
                    fill=fill, outline=outline,
                )
                canvas.text((sxx + 10, ry + 6), col, font=font(10, True),
                            fill=fg)

    image.save(OUT)
    print(f"PNG: {OUT}")
    print()
    print("订单着色：")
    for order in orders:
        palette = ORDER_PALETTE[order_color_index(order["order_id"])]
        seats = sorted(p["seat_id"] for p in order["passengers"]
                       if p.get("seat_id"))
        rows_used = sorted({s[:2] + "车" + s[3:5] + "排" for s in seats})
        print(f"  {order['order_id']:8} {palette['bg']}  {seats}  "
              f"{len(rows_used)} 排")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
