from lxml import etree
import cairosvg
from lxml.builder import E

try:
    from random import choices
except Exception:
    from random import choice

    def choices(population, k=1, *args, **kwargs):
        return [choice(population) for _ in range(k)]


from string import ascii_lowercase
from math import radians, sin, cos
from wand.image import Image
from io import StringIO

_converters = {}  ##  Dict[str, Callable[[etree.Element], etree.Element]]


def _conv(f):
    _converters[f.__name__] = f
    return f


def repl_attr_name(
    el, old_attr, new_attr=None, value_transform=lambda x: x, default=None
):
    v = el.attrib.pop(old_attr, None)

    if v is not None:
        el.attrib[new_attr or old_attr] = value_transform(v)
    elif default:
        el.attrib[new_attr or old_attr] = default


def remove_dp(x):
    return x.replace("dp", "").replace("dip", "")


def split_argb(argb):
    return int(argb[1:3], 16) / 255, "#{}".format(argb[3:])


@_conv
def gradient(el):
    a = el.attrib

    svg = el.getroottree().getroot()
    defs = (
        svg.find("defs") or svg.append(E.defs()) or svg.find("defs")
    )  # elements does not have setdefault :'(

    type_ = a.get("type", "linear")
    id_ = "".join(choices(ascii_lowercase, k=10))

    el.tag = {"linear": "linearGradient"}[type_]
    a["id"] = id_
    angle = a.pop("angle", None)
    if angle:
        angle_rad = radians(float(angle))
        x = cos(angle_rad) * 100
        y = cos(angle_rad) * 100
        # x1="0" y1="0" x2="1" y2="0.5"

        a["x1"] = "{}%".format((100 if x < 0 else 0))

        a["y1"] = "{}%".format(100 if y < 0 else 0)

        a["x2"] = "{}%".format(x if x >= 0 else 100 + x)
        a["y2"] = "{}%".format(x if y >= 0 else 100 + x)

    # stops
    stops = [
        (0, el.get("startColor", None)),
        (50, el.get("centerColor", None)),
        (100, el.get("endColor", None)),
    ]

    for percent, color in stops:
        if not color:
            continue
        # <stop offset="0%" style="stop-color: #906; stop-opacity: 1.0"/>
        a, rgb = split_argb(color)
        el.append(
            E.stop(
                offset="{}%".format(percent),
                style="stop-color: {}; stop-opacity: {}".format(rgb, a),
            )
        )

    parent = el.getparent()
    if parent.tag in ["svg", "shape"]:
        parent = E.rect(x="0%", y="0%", width="100%", height="100%")
        svg.append(parent)

    parent.set("style", "fill: url(#{})".format(id_))
    defs.append(el)


@_conv
def vector(el):
    a = el.attrib
    h = a.pop("viewportHeight")
    w = a.pop("viewportWidth")
    a["viewBox"] = "0 0 {} {}".format(h, w)

    repl_attr_name(el, "height", value_transform=remove_dp, default="480px")
    repl_attr_name(el, "width", value_transform=remove_dp, default="480px")


@_conv
def group(el):
    attr = el.attrib
    rotation = attr.pop("rotation", 0)
    pivot_x = attr.pop("pivotX", 0)
    pivot_y = attr.pop("pivotY", 0)
    scale_x = attr.pop("scaleX", 1)
    scale_y = attr.pop("scaleY", 1)
    translate_x = attr.pop("translateX", 0)
    translate_y = attr.pop("translateY", 0)

    # scale, rotate then translate.
    attr["transform"] = "scale({} {}) rotate({} {} {}) translate({} {})".format(
        scale_x, scale_y, rotation, pivot_x, pivot_y, translate_x, translate_y
    )


@_conv
def path(el):
    repl_attr_name(el, "pathData", "d")
    repl_attr_name(el, "strokeWidth", "stroke-width")
    repl_attr_name(el, "strokeColor", "stroke")
    repl_attr_name(el, "strokeLinecap", "stroke-linecap")
    repl_attr_name(el, "strokeLineJoin", "stroke-line-join")
    repl_attr_name(el, "strokeMiterLimit", "stroke-miter-limit")

    fill = el.attrib.pop("fillColor", None)
    if fill:
        if fill.startswith("#") and len(fill) == 9:
            el.attrib["fill-opacity"] = str(int(fill[1:3], 16) / 255.0)
            fill = "#{}".format(fill[-6:])
    el.attrib["fill"] = fill

    # missing translation
    # android:strokeAlpha
    #     The opacity of a path stroke. Default is 1.
    # android:fillAlpha
    #     The opacity to fill the path with. Default is 1.
    # android:trimPathStart
    #     The fraction of the path to trim from the start, in the range from 0
    #  to 1. Default is 0.
    # android:trimPathEnd
    #     The fraction of the path to trim from the end, in the range from 0
    #  to 1. Default is 1.
    # android:trimPathOffset
    # android:fillType
    # For SDK 24+, sets the fillType for a path. The types can be either
    # "evenOdd" or "nonZero". They behave the same as SVG's "fill-rule"
    #  properties. Default is nonZero. For more details, see FillRuleProperty


def transform(el):
    for a, v in el.items():
        del el.attrib[a]
        a = etree.QName(a).localname
        el.attrib[a] = v

    _converters.get(el.tag, lambda x: x)(el)


def vd2svg(input_file):
    for _, el in etree.iterparse(input_file):
        transform(el)
    # el is now the last element of the parsed tree (the root)
    # but lxml does not allow us to replace the nsmap so we need to «transplant» it
    root = etree.Element("svg", nsmap={"svg": "http://www.w3.org/2000/svg"})

    for ch in el:
        root.append(ch)

    for a, v in el.items():
        root.attrib[a] = v

    return etree.tostring(root)


def vd2png(input, output, scale):
    svg = vd2svg(input)
    with Image(blob=svg, format="svg", resolution=480) as img:
        img.format = "png"
        img.save(file=output)
