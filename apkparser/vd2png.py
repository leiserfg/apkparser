from math import cos, radians, sin
from string import ascii_lowercase

from lxml import etree
from lxml.builder import E
from wand.api import library
import wand.color
import wand.image
try:
    from random import choices
except Exception:
    from random import choice

    def choices(population, k=1, *args, **kwargs):
        return [choice(population) for _ in range(k)]


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


class Vd2PngConverter:
    def __init__(self, apk):
        self._res_parser = apk.get_android_resources()
        self._apk = apk
        # colors = res_parser.get_color_resources()

    def split_argb(self, argb: str):
        if len(argb) > 7:
            return int(argb[1:3], 16) / 255, "#{}".format(argb[3:])
        return 1, argb

    def conv_gradient(self, el):
        a = el.attrib

        svg = el.getroottree().getroot()
        defs = (
            svg.find("defs") or svg.append(E.defs()) or svg.find("defs")
        )  # elements does not have setdefault :'(
        types = ["linearGradient", "radialGradient", "sweepGradient"]
        # SweepGradient does not exist on svg but it will be here as a reminder that I need to
        # implement it
        type_ = int(a.get("type", 0))
        id_ = "".join(choices(ascii_lowercase, k=10))

        el.tag = types[type_]
        a["id"] = id_
        angle = a.pop("angle", None)
        if angle:
            angle_rad = radians(float(angle))
            x = cos(angle_rad) * 100
            y = sin(angle_rad) * 100
            # x1="0" y1="0" x2="1" y2="0.5"

            a["x1"] = "{}%".format((100 if x < 0 else 0))

            a["y1"] = "{}%".format(100 if y < 0 else 0)

            a["x2"] = "{}%".format(x if x >= 0 else 100 + x)
            a["y2"] = "{}%".format(x if y >= 0 else 100 + x)

        stops = [
            (0, el.get("startColor", None)),
            (50, el.get("centerColor", None)),
            (100, el.get("endColor", None)),
        ]
        for ch in el:
            # just in case the gradient is a loaded resource
            self.transform(ch)
        child_stops = ((float(ch.get("offset")) * 100, ch.get("color")) for ch in el)
        stops.extend(child_stops)
        for percent, color in stops:
            if not color:
                continue
            # <stop offset="0%" style="stop-color: #906; stop-opacity: 1.0"/>
            a, rgb = self.split_argb(color)
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

    def conv_group(self, el):
        el.tag = 'g'
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

    def conv_solid(self, el):
        el.tag = "rect"
        for k, v in dict(x="0%", y="0%", width="100%", height="100%").items():
            el.set(k, v)
        fill = el.attrib.pop("color", None)
        if fill:
            a, rgb = self.split_argb(fill)
            el.attrib["fill-opacity"] = str(a)
            fill = rgb
            el.attrib["fill"] = fill

    def conv_path(self, el):
        repl_attr_name(el, "pathData", "d")
        repl_attr_name(el, "strokeWidth", "stroke-width")
        repl_attr_name(el, "strokeColor", "stroke")
        repl_attr_name(el, "strokeLinecap", "stroke-linecap")
        repl_attr_name(el, "strokeLineJoin", "stroke-line-join")
        repl_attr_name(el, "strokeMiterLimit", "stroke-miter-limit")

        fill = el.attrib.pop("fillColor", None)
        if fill:
            if fill.startswith("#"):
                a, rgb = self.split_argb(fill)
                el.attrib["fill-opacity"] = str(a)
                fill = rgb
                el.attrib["fill"] = fill
            elif fill.startswith("@"):
                res = self._apk.get_resource_as_xml(fill)
                el.append(res)
                self.transform(res)

            else:
                raise ValueError("Unexpected fill value {}".format(fill))

        repl_attr_name(el, "fillAlpha", "fill-opacity")
        repl_attr_name(el, "strokeAlpha", "stroke-opacity")
        repl_attr_name(el, "fillType", "fill-rule", lambda x: x.lower())
        # missing translation
        # android:trimPathStart
        #     The fraction of the path to trim from the start, in the range from 0
        #  to 1. Default is 0.
        # android:trimPathEnd
        #     The fraction of the path to trim from the end, in the range from 0
        #  to 1. Default is 1.
        # android:trimPathOffset

    def transform(self, el):
        for a, v in el.items():
            del el.attrib[a]
            a = etree.QName(a).localname
            el.attrib[a] = v
        getattr(self, "conv_" + el.tag, lambda x: x)(el)

    def vd2svg(self, input_file):
        for _, el in etree.iterparse(input_file):
            self.transform(el)
        # el is now the last element of the parsed tree (the root)
        # but lxml does not allow us to replace the nsmap so we need to «transplant» it
        root = etree.Element("svg", nsmap={"svg": "http://www.w3.org/2000/svg"})

        for ch in el:
            root.append(ch)

        for a, v in el.items():
            root.attrib[a] = v

        a = root.attrib
        h = a.pop("viewportHeight", "480")
        w = a.pop("viewportWidth", "480")
        a["viewBox"] = "0 0 {} {}".format(h, w)

        repl_attr_name(root, "height", value_transform=remove_dp, default="480px")
        repl_attr_name(root, "width", value_transform=remove_dp, default="480px")

        return etree.tostring(root)

    def vd2png(self, input, output, scale):
        svg = self.vd2svg(input)
        with wand.image.Image() as image:
            with wand.color.Color('transparent') as background_color:
                library.MagickSetBackgroundColor(image.wand, background_color.resource)
            image.read(blob=svg, resolution=480)
            png_image = image.make_blob("png32")
            output.write(png_image)
