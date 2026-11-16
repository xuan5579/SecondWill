from PIL import Image, ImageDraw, ImageFilter

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

r = int(S * 0.22)
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=r, fill=(20, 22, 29, 255))

glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.ellipse([-S * 0.35, -S * 0.75, S * 1.35, S * 0.55], fill=(255, 255, 255, 16))
img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(S * 0.10)))
d = ImageDraw.Draw(img)

d.rounded_rectangle([6, 6, S - 7, S - 7], radius=r - 6, outline=(255, 255, 255, 22), width=6)

# Two bars of unequal height, bottom-aligned: reads as a signal, not a pause button.
bw = int(S * 0.115)
gap = int(S * 0.105)
x1 = int(S / 2 - gap / 2 - bw)
x2 = int(S / 2 + gap / 2)
br = bw // 2
bottom = S // 2 + int(S * 0.23)
h1 = int(S * 0.46)
h2 = int(S * 0.28)

d.rounded_rectangle([x1, bottom - h1, x1 + bw, bottom], radius=br, fill=(232, 236, 246, 255))
d.rounded_rectangle([x2, bottom - h2, x2 + bw, bottom], radius=br, fill=(255, 176, 106, 255))

img.save("assets/secondwill.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save("assets/secondwill.png")
print("ok")
