"""The published screenshots: the PCB editor with the Koppar window over its right side.

    python3 scripts/compose_shots.py <shots-dir> <out-dir> name=shot [name=shot ...]

Each shot is a pair the smoke or tour driver wrote with KOPPAR_SMOKE_COMPOSE=1: <shot>.png (the
Koppar window) and <shot>.editor.png (the editor). Writes <out-dir>/<name>.webp at 2188x1275 (the
site) and <out-dir>/<name>.png at 1600x932 (the README). Needs Pillow.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFilter

W, H = 2188, 1275
EDITOR_W = 1560          # the editor's left part that stays visible
MARGIN = 44              # from the window to the right and top edges
RADIUS = 14
WINDOW_SCALE = 0.92
SMALL = (1600, 932)


def rounded(img, r):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), r, fill=255)
    return mask


def compose(window_png, editor_png):
    canvas = Image.new("RGB", (W, H), "white")
    editor = Image.open(editor_png).convert("RGB")
    scale = H / editor.height
    editor = editor.resize((round(editor.width * scale), H), Image.LANCZOS).crop((0, 0, EDITOR_W, H))
    canvas.paste(editor, (0, 0))
    win = Image.open(window_png).convert("RGB")
    # a little smaller than life, so the board on the left stays clear of it
    s = min(WINDOW_SCALE, (H - 2 * MARGIN) / win.height)
    win = win.resize((round(win.width * s), round(win.height * s)), Image.LANCZOS)
    x, y = W - MARGIN - win.width, MARGIN
    shadow = Image.new("L", (win.width + 80, win.height + 80), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((40, 48, 40 + win.width, 48 + win.height), RADIUS, fill=90)
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    canvas.paste(Image.new("RGB", shadow.size, "black"), (x - 40, y - 40), shadow)
    canvas.paste(win, (x, y), rounded(win, RADIUS))
    ImageDraw.Draw(canvas).rounded_rectangle((x, y, x + win.width - 1, y + win.height - 1), RADIUS, outline=(214, 211, 209))
    return canvas


def main():
    src, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    for pair in sys.argv[3:]:
        name, shot = pair.split("=", 1)
        base = os.path.join(src, shot)
        img = compose(base + ".png", base + ".editor.png")
        img.save(os.path.join(out, f"{name}.webp"), quality=88, method=6)
        img.resize(SMALL, Image.LANCZOS).save(os.path.join(out, f"{name}.png"), optimize=True)
        print(name)


if __name__ == "__main__":
    main()
