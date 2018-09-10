from .vd2png import vd2png
from PIL import Image
from io import BytesIO
from .axml import AXMLPrinter


def layer_from_color(color):
    alpha = 255
    if len(color) > 6:  # ARGB
        color = color[1:]
        alpha = int(color[:2], 16)
        color = "#" + color[2:]  # RGB

    image = Image.new("RGB", (512, 512), color)
    image.putalpha(alpha)
    return image


def _axml2png(axml):
    out = BytesIO()
    vd2png(
        BytesIO(AXMLPrinter(axml).get_xml()), out, 10
    )  # scale to 10x for getting a hight quality icon
    return out


def build_icon(parts, output_path: str):

    layers = [
        layer_from_color(name)
        if name.startswith("#")
        else Image.open(
            _axml2png(content) if name.endswith(".xml") else BytesIO(content)
        )
        for name, content in parts
    ]

    if len(layers) == 1:
        icon = layers[0]
    else:
        layers = [l.convert("RGBA") for l in layers]
        min_size = min(layers, key=lambda x: x.size).size
        layers = [l if l.size == min_size else l.resize(min_size) for l in layers]
        icon = Image.alpha_composite(*layers)
    icon.save(output_path)
