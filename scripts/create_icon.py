"""Recreate the news/search app icon (Pillow is needed only for this script)."""
from pathlib import Path

from PIL import Image, ImageDraw


def main():
    destination = Path(__file__).resolve().parents[1] / 'assets'
    destination.mkdir(exist_ok=True)
    image = Image.new('RGBA', (1024, 1024))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((32, 32, 992, 992), radius=220, fill='#236b59')
    # A newspaper with a bold headline and a small column of news.
    draw.rounded_rectangle((204, 190, 746, 794), radius=54, fill='#ffffff')
    draw.rounded_rectangle((264, 258, 656, 304), radius=16, fill='#236b59')
    draw.rounded_rectangle((264, 352, 408, 510), radius=20, fill='#7ce0bd')
    for y in (356, 420, 484):
        draw.rounded_rectangle((448, y, 656, y + 24), radius=12, fill='#8da99f')
    for y in (566, 630, 694):
        draw.rounded_rectangle((264, y, 556, y + 24), radius=12, fill='#8da99f')
    # A high-contrast search lens remains readable at taskbar sizes.
    draw.line((718, 718, 838, 838), fill='#123c32', width=100)
    draw.ellipse((788, 788, 888, 888), fill='#123c32')
    draw.ellipse((538, 538, 826, 826), fill='#123c32')
    draw.ellipse((584, 584, 780, 780), fill='#7ce0bd')
    draw.arc((612, 612, 752, 752), 190, 265, fill='#ffffff', width=22)
    image = image.resize((512, 512), Image.Resampling.LANCZOS)
    image.save(destination / 'news-monitor.png')
    image.save(destination / 'news-monitor.ico', sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])


if __name__ == '__main__':
    main()
