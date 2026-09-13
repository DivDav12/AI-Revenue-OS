"""Branded pin/slideshow image rendering - real product photo + real price,
no fabricated visuals.

Pinterest pin drafting (`pinterest_pins.py`) only produces text metadata; a
human still needs an actual image file to upload. This module renders one
from data that already exists in the affiliate offer record - the real
Amazon product photo (`AffiliateOffer.image_urls`, hotlinked straight from
Amazon's own CDN, same compliant source `products.py` uses for the public
site) and the real, human-verified `product_price`. Nothing here invents a
product, a price, or a photo.

Pillow-only, no LLM call, $0 API cost - same discipline as the rest of the
affiliate pipeline. Output files are local PNGs for a human to post
themselves (Pinterest pin image, TikTok slideshow slide); this module never
uploads or posts anything.
"""

from __future__ import annotations

import io
import os
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .affiliate_model import AffiliateOffer

# ---------------------------------------------------------------------------
# brand + canvas constants
# ---------------------------------------------------------------------------

BRAND_NAME = "SmartFinds"

#: Pinterest's recommended pin ratio (2:3); also reads fine as a portrait
#: TikTok slideshow slide.
CANVAS_W, CANVAS_H = 1000, 1500

_BG = (239, 228, 204)       # --bg-page (cream), matches the public site
_PANEL = (255, 255, 255)    # --bg
_BORDER = (221, 214, 196)   # --border
_INK = (26, 26, 26)         # --fg
_MUTED = (102, 100, 96)     # --muted
_NAVY = (44, 62, 80)        # --accent
_BORDEAUX = (139, 46, 46)   # --accent-2

#: Soft top-to-bottom gradient the product pin floats on (a lightened tint
#: of the brand's own bordeaux accent, not an unrelated color).
_GRAD_TOP = (250, 242, 238)
_GRAD_BOTTOM = (234, 208, 202)

_PHOTO_TIMEOUT_S = 15
_USER_AGENT = "Mozilla/5.0 (compatible; SmartFindsPinRenderer/1.0)"

# Windows ships these; fall back to Pillow's built-in font if unavailable
# (e.g. running the test suite on a non-Windows box).
_FONT_DIR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Fonts"
_SERIF_BOLD = _FONT_DIR / "georgiab.ttf"
_SERIF = _FONT_DIR / "georgia.ttf"
_SANS_BOLD = _FONT_DIR / "arialbd.ttf"
_SANS = _FONT_DIR / "arial.ttf"


class PinImageError(RuntimeError):
    """Raised when a pin image cannot be rendered from real data (e.g. the
    product photo failed to download) - never silently replaced with a
    placeholder."""


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _download_photo(url: str) -> Image.Image:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_PHOTO_TIMEOUT_S) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PinImageError(f"could not download product photo {url!r}: {exc}") from exc
    return Image.open(io.BytesIO(data)).convert("RGB")


def _contain(photo: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Resize `photo` to fit inside box_w x box_h, preserving aspect ratio,
    centered on a white panel - never crops off part of the real product."""
    canvas = Image.new("RGB", (box_w, box_h), _PANEL)
    scale = min(box_w / photo.width, box_h / photo.height)
    new_size = (max(1, int(photo.width * scale)), max(1, int(photo.height * scale)))
    resized = photo.resize(new_size, Image.LANCZOS)
    canvas.paste(resized, ((box_w - new_size[0]) // 2, (box_h - new_size[1]) // 2))
    return canvas


def _price_text(offer: AffiliateOffer) -> str:
    symbol = {"EUR": "\u20ac", "USD": "$", "GBP": "\u00a3"}.get(offer.currency, offer.currency + " ")
    return f"{symbol}{offer.product_price:.2f}"


def _is_near_white(pixel: tuple, thresh: int) -> bool:
    r, g, b = pixel[0], pixel[1], pixel[2]
    return r >= 255 - thresh and g >= 255 - thresh and b >= 255 - thresh


def _cutout(photo: Image.Image, thresh: int = 24) -> Image.Image:
    """Remove a plain white/near-white studio background (how Amazon's own
    product photos are shot) so the product can float directly on our brand
    background instead of sitting in a boxed panel. Flood-fills only from
    the four corners, so a white logo or highlight in the middle of the
    product itself is never touched - never invents or alters product
    pixels, only clears background ones that are actually white."""
    rgba = photo.convert("RGBA")
    w, h = rgba.size
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        pixel = rgba.getpixel(seed)
        if pixel[3] == 0 or not _is_near_white(pixel, thresh):
            continue
        ImageDraw.floodfill(rgba, seed, (255, 255, 255, 0), thresh=thresh)
    return rgba


def _gradient_bg(w: int, h: int, top: tuple = _GRAD_TOP, bottom: tuple = _GRAD_BOTTOM) -> Image.Image:
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        row = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (w, y)], fill=row)
    return img


def _contain_rgba(photo: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Like `_contain`, but keeps transparency instead of filling a panel -
    for a cutout product photo floating on a colored background."""
    canvas = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    scale = min(box_w / photo.width, box_h / photo.height)
    new_size = (max(1, int(photo.width * scale)), max(1, int(photo.height * scale)))
    resized = photo.resize(new_size, Image.LANCZOS)
    pos = ((box_w - new_size[0]) // 2, (box_h - new_size[1]) // 2)
    canvas.paste(resized, pos, resized)
    return canvas


def _shadow_layer(canvas_w: int, canvas_h: int, content_box: tuple) -> Image.Image:
    """A soft blurred ellipse under `content_box` (the product's actual,
    non-transparent bounds) so it reads as floating rather than pasted."""
    shadow = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow)
    x0, y0, x1, y1 = content_box
    ellipse_w = int((x1 - x0) * 0.55)
    ellipse_h = max(14, int(ellipse_w * 0.12))
    cx = (x0 + x1) // 2
    cy = y1 - int(ellipse_h * 0.4)
    draw.ellipse(
        [cx - ellipse_w // 2, cy - ellipse_h // 2, cx + ellipse_w // 2, cy + ellipse_h // 2],
        fill=(30, 20, 20, 110),
    )
    return shadow.filter(ImageFilter.GaussianBlur(22))


def render_product_pin(offer: AffiliateOffer, *, out_path,
                       brand: str = BRAND_NAME) -> Path:
    """Render one Pinterest pin / TikTok slideshow slide for a real,
    verified affiliate offer. Raises `PinImageError` if the offer has no
    real photo or no real (non-estimate) price - a pin is never drawn
    against fabricated data.
    """
    if not offer.image_urls:
        raise PinImageError(f"offer {offer.offer_id!r} has no image_urls - nothing real to render")
    if offer.product_price <= 0:
        raise PinImageError(f"offer {offer.offer_id!r} has no verified product_price")

    photo = _download_photo(offer.image_urls[0])
    cutout = _cutout(photo)

    img = _gradient_bg(CANVAS_W, CANVAS_H)
    draw = ImageDraw.Draw(img)
    margin = 48

    # brand wordmark
    brand_font = _font(_SERIF_BOLD, 46)
    draw.text((margin, 44), brand, font=brand_font, fill=_NAVY)
    tag_font = _font(_SANS_BOLD, 22)
    draw.text((margin, 104), "AMAZON PICK", font=tag_font, fill=_BORDEAUX)

    # product photo, floating on the gradient with a soft drop shadow
    photo_top, photo_h = 160, 760
    box_w, box_h = CANVAS_W - 2 * margin, photo_h
    fitted = _contain_rgba(cutout, box_w, box_h)
    photo_x, photo_y = margin, photo_top

    bbox = fitted.getbbox() or (0, 0, box_w, box_h)
    content_box = (photo_x + bbox[0], photo_y + bbox[1], photo_x + bbox[2], photo_y + bbox[3])
    shadow = _shadow_layer(CANVAS_W, CANVAS_H, content_box)
    img.paste(shadow, (0, 0), shadow)
    img.paste(fitted, (photo_x, photo_y), fitted)

    # product name
    name_font = _font(_SERIF_BOLD, 44)
    name_top = photo_top + photo_h + 44
    max_text_w = CANVAS_W - 2 * margin
    lines = _wrap(draw, offer.product_name, name_font, max_text_w)[:3]
    y = name_top
    for line in lines:
        draw.text((margin, y), line, font=name_font, fill=_INK)
        y += 54

    # price, plain (no box) to match the airier layout
    price_font = _font(_SANS_BOLD, 48)
    price_str = _price_text(offer)
    y += 14
    draw.text((margin, y), price_str, font=price_font, fill=_BORDEAUX)
    price_w = draw.textlength(price_str, font=price_font)
    on_amazon_font = _font(_SANS, 26)
    draw.text((margin + price_w + 18, y + 12), "on Amazon", font=on_amazon_font, fill=_MUTED)

    disclosure_font = _font(_SANS, 18)
    draw.text((margin, CANVAS_H - margin - 22), "Affiliate link \u00b7 Werbung",
             font=disclosure_font, fill=_MUTED)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def render_outro_slide(*, headline: str, link_text: str, subhead: str = "",
                       out_path, brand: str = BRAND_NAME) -> Path:
    """Render a text-only closing slide (e.g. a TikTok slideshow's last
    slide) telling the viewer where to find the products - never a product
    photo/price itself, just the same brand treatment as the cover slide."""
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), _BG)
    draw = ImageDraw.Draw(img)
    margin = 48
    max_text_w = CANVAS_W - 2 * margin

    brand_font = _font(_SERIF_BOLD, 40)
    draw.text((margin, 44), brand, font=brand_font, fill=_NAVY)

    headline_font = _font(_SERIF_BOLD, 66)
    lines = _wrap(draw, headline.upper(), headline_font, max_text_w)
    y = CANVAS_H // 2 - 40 * len(lines)
    for line in lines:
        draw.text((margin, y), line, font=headline_font, fill=_INK)
        y += 80

    if subhead:
        subhead_font = _font(_SANS_BOLD, 30)
        draw.text((margin, y + 12), subhead, font=subhead_font, fill=_MUTED)
        y += 60

    link_font = _font(_SANS_BOLD, 40)
    draw.text((margin, y + 40), link_text, font=link_font, fill=_BORDEAUX)

    disclosure_font = _font(_SANS, 18)
    draw.text((margin, CANVAS_H - margin - 22), "Affiliate links · Werbung",
             font=disclosure_font, fill=_MUTED)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path


def render_cover_slide(offers: list[AffiliateOffer], *, headline: str, subhead: str,
                       out_path, brand: str = BRAND_NAME) -> Path:
    """Render a text-forward cover slide (e.g. a TikTok slideshow's first
    slide) with a strip of the real product photos behind the headline.
    Never posted to Pinterest by this module - callers decide distribution.
    """
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), _BG)
    draw = ImageDraw.Draw(img)
    margin = 48

    brand_font = _font(_SERIF_BOLD, 40)
    draw.text((margin, 44), brand, font=brand_font, fill=_NAVY)

    headline_font = _font(_SERIF_BOLD, 74)
    max_text_w = CANVAS_W - 2 * margin
    lines = _wrap(draw, headline.upper(), headline_font, max_text_w)
    y = 260
    for line in lines:
        draw.text((margin, y), line, font=headline_font, fill=_INK)
        y += 88

    subhead_font = _font(_SANS_BOLD, 34)
    draw.text((margin, y + 12), subhead, font=subhead_font, fill=_BORDEAUX)

    # a row of real product thumbnails, up to 6, along the bottom third
    thumbs = [o for o in offers if o.image_urls][:6]
    if thumbs:
        strip_top = CANVAS_H - 430
        strip_h = 380
        gap = 18
        n = len(thumbs)
        thumb_w = (CANVAS_W - 2 * margin - (n - 1) * gap) // n
        x = margin
        for offer in thumbs:
            try:
                photo = _download_photo(offer.image_urls[0])
            except PinImageError:
                continue
            panel_box = (x, strip_top, x + thumb_w, strip_top + strip_h)
            draw.rectangle(panel_box, fill=_PANEL, outline=_BORDER, width=2)
            fit = _contain(photo, thumb_w - 16, strip_h - 16)
            img.paste(fit, (x + 8, strip_top + 8))
            x += thumb_w + gap

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path
