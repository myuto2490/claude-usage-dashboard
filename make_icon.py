# -*- coding: utf-8 -*-
"""
アプリアイコン生成スクリプト。

ダッシュボードの見た目 (ダークマテリアル + アクティビティリング) に合わせた
Apple 風の角丸スクエアアイコンを描画し、app_icon.ico を出力する。

    py make_icon.py
"""

import math
from PIL import Image, ImageDraw

S = 1024  # 作業キャンバス (高解像度で描いて縮小)


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_rgb(c1, c2, t):
    return tuple(int(lerp(a, b, t)) for a, b in zip(c1, c2))


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def build(size=S):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # --- 背景: ダークマテリアルの縦グラデーション ---
    top, bottom = (44, 44, 46), (18, 18, 20)
    for y in range(size):
        d.line([(0, y), (size, y)], fill=lerp_rgb(top, bottom, y / size) + (255,))

    # 上端のハイライト (ガラス感)
    hl = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    for y in range(int(size * 0.42)):
        a = int(26 * (1 - y / (size * 0.42)))
        hd.line([(0, y), (size, y)], fill=(255, 255, 255, a))
    img = Image.alpha_composite(img, hl)
    d = ImageDraw.Draw(img)

    # --- アクティビティリング ---
    cx = cy = size / 2
    r = size * 0.295          # リング半径
    w = size * 0.105          # リング太さ
    box = [cx - r, cy - r, cx + r, cy + r]

    # トラック (未達部分): 同系色の淡いステップ
    d.arc(box, start=0, end=360, fill=(214, 138, 92, 56), width=int(w))

    # 塗り: Claude オレンジのグラデーション弧 (約78%)
    start_deg, sweep = -90, 280
    c_from, c_to = (232, 158, 108), (201, 100, 42)
    steps = 240
    for i in range(steps):
        t = i / steps
        a0 = start_deg + sweep * t
        a1 = start_deg + sweep * (i + 1) / steps + 0.6  # 継ぎ目消し
        d.arc(box, start=a0, end=a1, fill=lerp_rgb(c_from, c_to, t) + (255,), width=int(w))

    # 丸キャップ (両端)
    for t, col in ((0.0, c_from), (1.0, c_to)):
        ang = math.radians(start_deg + sweep * t)
        ex, ey = cx + r * math.cos(ang), cy + r * math.sin(ang)
        rr = w / 2
        d.ellipse([ex - rr, ey - rr, ex + rr, ey + rr], fill=col + (255,))

    # --- Apple 風角丸 (約22.4%) で切り抜き ---
    img.putalpha(rounded_mask(size, int(size * 0.224)))
    return img


def main():
    base = build()
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [base.resize((s, s), Image.LANCZOS) for s in sizes]
    imgs[-1].save(
        "app_icon.ico",
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=imgs[:-1],
    )
    base.resize((256, 256), Image.LANCZOS).save("app_icon_preview.png")
    print("app_icon.ico written")


if __name__ == "__main__":
    main()
