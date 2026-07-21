#!/usr/bin/env python3
"""
Generate Vigil's app icon: a white shield with a keyhole on a blue→indigo
squircle. Renders each iconset size natively (crisp), then you run `iconutil`.

    python3 tools/make_icon.py assets/Vigil.iconset
    iconutil -c icns assets/Vigil.iconset -o assets/Vigil.icns
"""
import os
import sys
import math

import Quartz
from Quartz import (CGColorSpaceCreateDeviceRGB, CGBitmapContextCreate,
                    CGBitmapContextCreateImage, CGContextSaveGState,
                    CGContextRestoreGState, CGContextSetRGBFillColor,
                    CGContextAddPath, CGContextClip, CGContextFillPath,
                    CGContextBeginPath, CGContextMoveToPoint,
                    CGContextAddLineToPoint, CGContextAddCurveToPoint,
                    CGContextAddQuadCurveToPoint, CGContextClosePath,
                    CGGradientCreateWithColorComponents,
                    CGContextDrawLinearGradient, CGPathCreateMutable,
                    CGPathMoveToPoint, CGPathAddLineToPoint,
                    CGPathAddCurveToPoint, CGPathAddQuadCurveToPoint,
                    CGPathCloseSubpath, CGPathCreateWithRoundedRect,
                    CGRectMake, CGContextFillRect, CGImageDestinationCreateWithURL,
                    CGImageDestinationAddImage, CGImageDestinationFinalize)
from CoreFoundation import CFURLCreateWithFileSystemPath, kCFURLPOSIXPathStyle


def _grad(cs, c0, c1):
    comps = list(c0) + list(c1)
    return CGGradientCreateWithColorComponents(cs, comps, [0.0, 1.0], 2)


def _shield_path(cx, cy, w, h):
    p = CGPathCreateMutable()
    left, right = cx - w / 2, cx + w / 2
    top, bot = cy + h / 2, cy - h / 2
    r = w * 0.11
    CGPathMoveToPoint(p, None, left + r, top)
    CGPathAddLineToPoint(p, None, right - r, top)
    CGPathAddQuadCurveToPoint(p, None, right, top, right, top - r)
    CGPathAddLineToPoint(p, None, right, cy - h * 0.02)
    CGPathAddCurveToPoint(p, None, right, bot + h * 0.30,
                          cx + w * 0.24, bot + h * 0.07, cx, bot)
    CGPathAddCurveToPoint(p, None, cx - w * 0.24, bot + h * 0.07,
                          left, bot + h * 0.30, left, cy - h * 0.02)
    CGPathAddLineToPoint(p, None, left, top - r)
    CGPathAddQuadCurveToPoint(p, None, left, top, left + r, top)
    CGPathCloseSubpath(p)
    return p


def _keyhole_path(cx, cy, s):
    """Keyhole: a circle with a tapered slot overlapping below it."""
    p = CGPathCreateMutable()
    kr = s * 0.52
    ccy = cy + s * 0.42                 # circle center
    k = 0.5523 * kr
    CGPathMoveToPoint(p, None, cx, ccy + kr)
    CGPathAddCurveToPoint(p, None, cx + k, ccy + kr, cx + kr, ccy + k, cx + kr, ccy)
    CGPathAddCurveToPoint(p, None, cx + kr, ccy - k, cx + k, ccy - kr, cx, ccy - kr)
    CGPathAddCurveToPoint(p, None, cx - k, ccy - kr, cx - kr, ccy - k, cx - kr, ccy)
    CGPathAddCurveToPoint(p, None, cx - kr, ccy + k, cx - k, ccy + kr, cx, ccy + kr)
    CGPathCloseSubpath(p)
    # tapered slot, overlapping the circle so they read as one keyhole
    slot_top = ccy - kr * 0.25
    slot_bot = cy - s * 1.15
    wt, wb = kr * 0.60, kr * 0.24
    CGPathMoveToPoint(p, None, cx - wt, slot_top)
    CGPathAddLineToPoint(p, None, cx - wb, slot_bot)
    CGPathAddLineToPoint(p, None, cx + wb, slot_bot)
    CGPathAddLineToPoint(p, None, cx + wt, slot_top)
    CGPathCloseSubpath(p)
    return p


def render(size, out_path):
    cs = CGColorSpaceCreateDeviceRGB()
    ctx = CGBitmapContextCreate(None, size, size, 8, 0, cs,
                                Quartz.kCGImageAlphaPremultipliedLast)
    S = float(size)

    # --- squircle background with blue→indigo gradient ---
    margin = S * 0.085
    side = S - 2 * margin
    corner = side * 0.2237
    bg = CGPathCreateWithRoundedRect(
        CGRectMake(margin, margin, side, side), corner, corner, None)
    CGContextSaveGState(ctx)
    CGContextAddPath(ctx, bg)
    CGContextClip(ctx)
    g = _grad(cs, (0.231, 0.510, 0.965, 1.0),   # #3B82F6 sky-blue (top)
                  (0.310, 0.275, 0.898, 1.0))   # #4F46E5 indigo   (bottom)
    CGContextDrawLinearGradient(ctx, g, (0, S), (0, 0), 0)
    CGContextRestoreGState(ctx)

    # --- shield (white, subtle depth gradient) ---
    cx, cy = S / 2, S * 0.505
    sw, sh = S * 0.46, S * 0.55
    shield = _shield_path(cx, cy, sw, sh)
    CGContextSaveGState(ctx)
    CGContextAddPath(ctx, shield)
    CGContextClip(ctx)
    gw = _grad(cs, (1.0, 1.0, 1.0, 1.0), (0.902, 0.925, 1.0, 1.0))  # white→#E6ECFF
    CGContextDrawLinearGradient(ctx, gw, (0, cy + sh / 2), (0, cy - sh / 2), 0)
    CGContextRestoreGState(ctx)

    # --- keyhole cut (mid indigo, reads as a through-hole) ---
    kh = _keyhole_path(cx, cy - sh * 0.03, sw * 0.34)
    CGContextSaveGState(ctx)
    CGContextAddPath(ctx, kh)
    CGContextClip(ctx)
    CGContextSetRGBFillColor(ctx, 0.271, 0.392, 0.933, 1.0)  # #4563EE
    CGContextFillRect(ctx, CGRectMake(0, 0, S, S))
    CGContextRestoreGState(ctx)

    img = CGBitmapContextCreateImage(ctx)
    url = CFURLCreateWithFileSystemPath(None, out_path, kCFURLPOSIXPathStyle, False)
    dest = CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    CGImageDestinationAddImage(dest, img, None)
    CGImageDestinationFinalize(dest)


def main():
    iconset = sys.argv[1] if len(sys.argv) > 1 else "assets/Vigil.iconset"
    os.makedirs(iconset, exist_ok=True)
    sizes = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
             (256, 1), (256, 2), (512, 1), (512, 2)]
    for base, scale in sizes:
        px = base * scale
        name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
        render(px, os.path.join(iconset, name))
    # also a standalone 1024 preview
    render(1024, os.path.join(os.path.dirname(iconset), "vigil_icon_preview.png"))
    print(f"wrote iconset -> {iconset}")


if __name__ == "__main__":
    main()
