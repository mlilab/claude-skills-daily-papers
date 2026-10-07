#!/usr/bin/env python3
"""Rasterize SVG figures to PNG previews with the system librsvg + cairo (via ctypes).

The previews only exist so the summarizing model can *look* at SVG plots (the Read tool shows
PNG/JPG, not SVG); the HTML pages keep using the original SVG. Text inside <foreignObject>
is not drawn by librsvg, so some labels may be missing in previews.

usage: svg_preview.py in.svg out.png [--width 1400]
"""
from __future__ import annotations

import ctypes
import ctypes.util
import sys

_libs = None


class _RsvgRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("width", ctypes.c_double), ("height", ctypes.c_double)]


class _RsvgDim(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int),
                ("em", ctypes.c_double), ("ex", ctypes.c_double)]


def _load():
    global _libs
    if _libs is None:
        names = {"rsvg": ["rsvg-2", "librsvg-2.so.2"], "cairo": ["cairo", "libcairo.so.2"],
                 "gobject": ["gobject-2.0", "libgobject-2.0.so.0"]}
        libs = {}
        for key, cands in names.items():
            for c in cands:
                path = ctypes.util.find_library(c) or c
                try:
                    libs[key] = ctypes.CDLL(path)
                    break
                except OSError:
                    continue
            else:
                _libs = False
                return None
        r, c = libs["rsvg"], libs["cairo"]
        r.rsvg_handle_new_from_file.restype = ctypes.c_void_p
        r.rsvg_handle_new_from_file.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        c.cairo_image_surface_create.restype = ctypes.c_void_p
        c.cairo_image_surface_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        c.cairo_create.restype = ctypes.c_void_p
        c.cairo_create.argtypes = [ctypes.c_void_p]
        c.cairo_set_source_rgb.argtypes = [ctypes.c_void_p] + [ctypes.c_double] * 3
        c.cairo_paint.argtypes = [ctypes.c_void_p]
        c.cairo_scale.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
        c.cairo_surface_write_to_png.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        c.cairo_destroy.argtypes = [ctypes.c_void_p]
        c.cairo_surface_destroy.argtypes = [ctypes.c_void_p]
        libs["gobject"].g_object_unref.argtypes = [ctypes.c_void_p]
        _libs = libs
    return _libs or None


def available() -> bool:
    return _load() is not None


def render(svg_path: str, png_path: str, width: int = 1400) -> bool:
    libs = _load()
    if not libs:
        return False
    r, c, g = libs["rsvg"], libs["cairo"], libs["gobject"]
    h = r.rsvg_handle_new_from_file(str(svg_path).encode(), None)
    if not h:
        return False
    try:
        w0 = ctypes.c_double()
        h0 = ctypes.c_double()
        ok = False
        if hasattr(r, "rsvg_handle_get_intrinsic_size_in_pixels"):
            fn = r.rsvg_handle_get_intrinsic_size_in_pixels
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)]
            ok = bool(fn(h, ctypes.byref(w0), ctypes.byref(h0)))
        if not ok or w0.value <= 0:
            dim = _RsvgDim()
            r.rsvg_handle_get_dimensions.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RsvgDim)]
            r.rsvg_handle_get_dimensions(h, ctypes.byref(dim))
            w0.value, h0.value = dim.width, dim.height
        if w0.value <= 0 or h0.value <= 0:
            return False
        scale = width / w0.value
        W, H = int(width), max(1, int(h0.value * scale))
        surf = c.cairo_image_surface_create(0, W, H)  # CAIRO_FORMAT_ARGB32
        cr = c.cairo_create(surf)
        c.cairo_set_source_rgb(cr, 1.0, 1.0, 1.0)
        c.cairo_paint(cr)
        if hasattr(r, "rsvg_handle_render_document"):
            r.rsvg_handle_render_document.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                      ctypes.POINTER(_RsvgRect), ctypes.c_void_p]
            vp = _RsvgRect(0, 0, W, H)
            r.rsvg_handle_render_document(h, cr, ctypes.byref(vp), None)
        else:
            c.cairo_scale(cr, scale, scale)
            r.rsvg_handle_render_cairo.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            r.rsvg_handle_render_cairo(h, cr)
        status = c.cairo_surface_write_to_png(surf, str(png_path).encode())
        c.cairo_destroy(cr)
        c.cairo_surface_destroy(surf)
        return status == 0
    finally:
        g.g_object_unref(h)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    width = int(sys.argv[sys.argv.index("--width") + 1]) if "--width" in sys.argv else 1400
    sys.exit(0 if render(sys.argv[1], sys.argv[2], width) else 1)
