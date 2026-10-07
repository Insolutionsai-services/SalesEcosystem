"""Campaign posters: a square 1080x1080 image (WhatsApp, Instagram, Facebook) drawn from a brand template.
The Creative agent writes the words (headline, offer line, call to action); this module lays them out with the
company's name and colours. No image model needed, so it is free and always legible."""
import secrets
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..core.config import MEDIA_DIR
from ..core.models import Tenant
from ..core.playbook import setting

SIZE = 1080
PAD = 84
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOAD_TYPES = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def _rgb(hex_color: str, fallback: str) -> tuple[int, int, int]:
    h = (hex_color or fallback).lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return _rgb(fallback, fallback)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, max_lines: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,") + "…"
    return lines


def _shade(rgb, k: float):
    return tuple(max(0, min(255, int(c * k))) for c in rgb)


def media_path(name: str) -> Path:
    return Path(MEDIA_DIR) / name


def new_name(ext: str) -> str:
    """Unguessable file name: images are served without a login so they can show in chats and posts."""
    return f"{secrets.token_urlsafe(12)}.{ext}"


def render(tenant: Tenant, headline: str, subline: str, cta: str) -> str:
    """Draws the poster and returns its file name under MEDIA_DIR."""
    primary = _rgb(setting(tenant, "brand.primary"), "#0a7a6a")
    accent = _rgb(setting(tenant, "brand.accent"), "#f2a443")
    ink, soft = (255, 255, 255), (228, 240, 236)
    img = Image.new("RGB", (SIZE, SIZE), primary)
    d = ImageDraw.Draw(img)
    # quiet background shapes in darker/lighter tones of the brand colour
    d.ellipse((SIZE - 420, -260, SIZE + 260, 420), fill=_shade(primary, 1.18))
    d.ellipse((-300, SIZE - 330, 380, SIZE + 350), fill=_shade(primary, .82))
    # brand mark + company name
    d.rounded_rectangle((PAD, PAD, PAD + 72, PAD + 72), radius=18, fill=accent)
    initial = (tenant.name.strip()[:1] or "S").upper()
    f_mark = _font(44)
    w = d.textlength(initial, font=f_mark)
    d.text((PAD + 36 - w / 2, PAD + 12), initial, font=f_mark, fill=primary)
    d.text((PAD + 96, PAD + 18), tenant.name, font=_font(38), fill=ink)
    # headline + offer line, centred vertically between the brand row and the button
    width = SIZE - 2 * PAD
    f_head = _font(104 if len(headline) < 22 else 84 if len(headline) < 40 else 70)
    f_sub = _font(46)
    head = _wrap(d, headline, f_head, width, 3)
    sub = _wrap(d, subline, f_sub, width, 4)
    block = len(head) * int(f_head.size * 1.1) + 40 + len(sub) * int(f_sub.size * 1.32)
    y = max(260, (SIZE - block) // 2 - 10)
    d.rounded_rectangle((PAD, y - 42, PAD + 96, y - 32), radius=5, fill=accent)
    for line in head:  # the built-in font has one weight; a thin stroke in the same colour reads as bold
        d.text((PAD, y), line, font=f_head, fill=ink, stroke_width=2, stroke_fill=ink)
        y += int(f_head.size * 1.1)
    y += 40
    for line in sub:
        d.text((PAD, y), line, font=f_sub, fill=soft)
        y += int(f_sub.size * 1.32)
    # call to action pill
    f_cta = _font(40)
    label = cta.strip() or "Reply to order"
    cw = d.textlength(label, font=f_cta)
    top = SIZE - PAD - 96
    d.rounded_rectangle((PAD, top, PAD + cw + 80, top + 96), radius=48, fill=accent)
    d.text((PAD + 40, top + 26), label, font=f_cta, fill=primary)
    contact = setting(tenant, "brand.contact")
    if contact:
        f_contact = _font(34)
        cw2 = d.textlength(contact, font=f_contact)
        d.text((SIZE - PAD - cw2, top + 30), contact, font=f_contact, fill=soft)
    name = new_name("png")
    path = media_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return name


def save_upload(data: bytes) -> str:
    """Stores a poster the company designed itself. Raises ValueError if it isn't a usable image."""
    if not data:
        raise ValueError("the file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("images must be 5 MB or smaller")
    from io import BytesIO
    try:
        with Image.open(BytesIO(data)) as im:
            fmt = im.format
            im.verify()
    except Exception as e:  # Pillow raises many types for broken files
        raise ValueError("that file isn't a PNG, JPEG or WebP image") from e
    if fmt not in UPLOAD_TYPES:
        raise ValueError("use a PNG, JPEG or WebP image")
    name = new_name(UPLOAD_TYPES[fmt])
    path = media_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return name
