from typing import Callable, Dict
from lxml import etree
import cairosvg

_converters: Dict[str, Callable[[etree.Element], etree.Element]] = {}


def _conv(f):
    _converters[f.__name__] = f
    return f


def repl_attr_name(el, old_attr, new_attr, value_transform=lambda x: x):
    v = el.attrib.pop(old_attr, None)
    if v is not None:
        el.attrib[new_attr] = value_transform(v)


def remove_dp(x):
    return x.replace("dp", "").replace("dip", "")


@_conv
def vector(el):
    a = el.attrib
    h = a.pop("viewportHeight")
    w = a.pop("viewportWidth")
    a["viewBox"] = "0 0 {} {}".format(h, w)

    repl_attr_name(el, "height", "height", remove_dp)
    repl_attr_name(el, "width", "width", remove_dp)


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

    return etree.tostring(root).decode("utf-8")


def vg2png(input, output):
    svg = vd2svg(input)
    cairosvg.svg2png(bytearray=svg, write_to=input)
