from vg2png import vg2png
from PIL import Image


def build_icon(fg_path: str, bg_path: str, output_path: str):
    bgImage = (
        Image.open(vg2png(bg_path)) if bg_path.endswith(".xml") else Image.open(bg_path)
    )
    fgImage = (
        Image.open(vg2png(fg_path)) if fg_path.endswith(".xml") else Image.open(fg_path)
    )
    merged = Image.alpha_composite(fgImage, fgImage)
    merged.save(output_path)
