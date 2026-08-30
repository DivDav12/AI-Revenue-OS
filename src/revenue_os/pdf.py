"""A minimal, dependency-free PDF writer for text documents.

The project is standard-library only (`pyproject` has no dependencies),
so rather than pull in reportlab / weasyprint / fpdf we emit a small,
valid PDF 1.4 by hand: A4 pages, the built-in Helvetica / Helvetica-Bold
/ Helvetica-Oblique / Courier fonts (no embedding), greedy word wrap
using the real AFM metrics, and automatic page breaks.

`render_markdown_pdf(md)` renders the subset of Markdown that
`deliverable.render_launch_plan_md` produces: `#`/`##`/`###` headings,
paragraphs, `-` bullets, `N.` numbered items, ``` fenced code, `---`
rules, and inline `**bold**` / `` `code` ``.

Not a general typesetter - just enough for a clean, readable customer
deliverable with zero install friction.
"""

from __future__ import annotations

import re
import zlib

# --- page geometry (points; 1/72 inch) --------------------------------
PAGE_W, PAGE_H = 595.28, 841.89          # A4
MARGIN = 56.0
CONTENT_W = PAGE_W - 2 * MARGIN
TOP_Y = PAGE_H - MARGIN
BOTTOM_Y = MARGIN

# style -> (font resource name, size, leading)
_STYLES = {
    "h1":   ("F2", 20.0, 27.0),
    "h2":   ("F2", 15.0, 21.0),
    "h3":   ("F2", 12.5, 17.0),
    "body": ("F1", 10.5, 15.0),
    "ital": ("F3", 10.5, 15.0),
    "code": ("F4", 9.0, 13.0),
    "bold": ("F2", 10.5, 15.0),
}
_FONT_BASE = {"F1": "Helvetica", "F2": "Helvetica-Bold",
              "F3": "Helvetica-Oblique", "F4": "Courier"}

# AFM advance widths (units/1000) for ASCII 32..126.
_HELV = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278,
    278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584,
    584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556,
    833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278,
    278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222,
    500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500,
    500, 334, 260, 334, 584,
]
_HELV_BOLD = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278,
    278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584,
    584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611,
    833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333,
    278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278,
    556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556,
    500, 389, 280, 389, 584,
]
_COURIER_W = 600  # monospace


def _char_width(ch: str, font: str, size: float) -> float:
    o = ord(ch)
    if font == "F4":
        w = _COURIER_W
    elif 32 <= o <= 126:
        table = _HELV_BOLD if font == "F2" else _HELV
        w = table[o - 32]
    else:
        w = 556
    return w / 1000.0 * size


def _text_width(text: str, font: str, size: float) -> float:
    return sum(_char_width(c, font, size) for c in text)


_TRANSLIT = {
    "–": "-", "—": "-", "−": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "•": "-", "…": "...", " ": " ",
    "→": "->", "←": "<-", "€": "EUR", "é": "e", "™": "(TM)",
    "«": '"', "»": '"', "\t": "    ",
}


def _ascii(text: str) -> str:
    out = []
    for ch in str(text):
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif 32 <= ord(ch) <= 126 or ch == "\n":
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


# --- inline run tokenising (**bold**, `code`) -------------------------

_INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def _runs(line: str, base_style: str) -> list[tuple[str, str]]:
    """Split a line into (style, text) runs. base_style is the run style
    for un-marked text (e.g. 'body', 'h2', 'ital')."""
    parts = _INLINE.split(line)
    runs: list[tuple[str, str]] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            runs.append(("bold", part[2:-2]))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            runs.append(("code", part[1:-1]))
        else:
            runs.append((base_style, part))
    return runs or [(base_style, "")]


def _wrap(runs: list[tuple[str, str]], base_style: str,
          max_width: float) -> list[list[tuple[str, str, float, float]]]:
    """Greedy word wrap across styled runs. Returns lines; each line is a
    list of (font, text, size, width) segments."""
    words: list[tuple[str, str]] = []   # (style, word) ; word includes no spaces
    for style, text in runs:
        chunks = text.split(" ")
        for i, chunk in enumerate(chunks):
            if chunk == "" and i not in (0, len(chunks) - 1):
                continue
            words.append((style, chunk))
            if i != len(chunks) - 1:
                words.append((base_style, " "))

    lines: list[list[tuple[str, str, float, float]]] = []
    cur: list[tuple[str, str, float, float]] = []
    cur_w = 0.0
    for style, word in words:
        font, size, _ = _STYLES.get(style, _STYLES[base_style])
        # hard-break a single word longer than the whole line
        while True:
            w = _text_width(word, font, size)
            if w <= max_width or not word:
                break
            keep = word
            while keep and _text_width(keep, font, size) > max_width:
                keep = keep[:-1]
            if not keep:
                break
            if cur:
                lines.append(cur)
                cur, cur_w = [], 0.0
            lines.append([(font, keep, size, _text_width(keep, font, size))])
            word = word[len(keep):]
        if word == " " and not cur:
            continue
        w = _text_width(word, font, size)
        if cur and cur_w + w > max_width:
            # trim a trailing space
            if cur and cur[-1][1] == " ":
                cur = cur[:-1]
            lines.append(cur)
            cur, cur_w = [], 0.0
            if word == " ":
                continue
        cur.append((font, word, size, w))
        cur_w += w
    if cur:
        if cur[-1][1] == " ":
            cur = cur[:-1]
        lines.append(cur)
    return lines or [[]]


# --- document model --------------------------------------------------

class _Page:
    def __init__(self) -> None:
        self.ops: list[str] = []

    def text(self, x: float, y: float, segments) -> None:
        if not segments:
            return
        self.ops.append("BT")
        self.ops.append(f"1 0 0 1 {x:.2f} {y:.2f} Tm")
        last_font = last_size = None
        for font, txt, size, _width in segments:
            if font != last_font or size != last_size:
                self.ops.append(f"/{font} {size:.2f} Tf")
                last_font, last_size = font, size
            self.ops.append(f"({_pdf_escape(txt)}) Tj")
        self.ops.append("ET")

    def rule(self, y: float) -> None:
        self.ops.append(
            f"0.6 w {MARGIN:.2f} {y:.2f} m {PAGE_W - MARGIN:.2f} {y:.2f} l S"
        )

    def stream(self) -> bytes:
        return ("\n".join(self.ops) + "\n").encode("latin-1", "replace")


class PdfDoc:
    """Flow text/blocks down A4 pages, break automatically, emit bytes."""

    def __init__(self) -> None:
        self.pages: list[_Page] = [_Page()]
        self.y = TOP_Y

    # -- low level --
    def _page(self) -> _Page:
        return self.pages[-1]

    def _newpage(self) -> None:
        self.pages.append(_Page())
        self.y = TOP_Y

    def space(self, pts: float) -> None:
        self.y -= pts
        if self.y <= BOTTOM_Y:
            self._newpage()

    def _need(self, pts: float) -> None:
        if self.y - pts <= BOTTOM_Y:
            self._newpage()

    # -- blocks --
    def paragraph(self, text: str, style: str = "body", *,
                  indent: float = 0.0, bullet: str = "") -> None:
        font, size, leading = _STYLES[style]
        avail = CONTENT_W - indent
        runs = _runs(_ascii(text), style)
        wrapped = _wrap(runs, style, avail)
        for i, segs in enumerate(wrapped):
            self._need(leading)
            x = MARGIN + indent
            if i == 0 and bullet:
                bf, bsz, _ = _STYLES["body"]
                bw = _text_width(bullet + " ", bf, bsz)
                self._page().text(MARGIN + max(0.0, indent - bw), self.y,
                                  [(bf, bullet + " ", bsz,
                                    _text_width(bullet + " ", bf, bsz))])
            self._page().text(x, self.y, segs)
            self.y -= leading

    def heading(self, text: str, level: int) -> None:
        style = {1: "h1", 2: "h2", 3: "h3"}.get(level, "h3")
        _, size, leading = _STYLES[style]
        self.space(10.0 if level == 1 else 8.0)
        # keep the heading with at least one following line
        self._need(leading * 2)
        self.paragraph(text, style)
        self.y -= 3.0

    def rule(self) -> None:
        self.space(8.0)
        self._need(6.0)
        self._page().rule(self.y)
        self.y -= 8.0

    def code_block(self, lines: list[str]) -> None:
        font, size, leading = _STYLES["code"]
        self.space(4.0)
        for raw in lines:
            text = _ascii(raw).rstrip("\n")
            # wrap long code lines on width, no word logic
            while True:
                self._need(leading)
                fit = text
                while fit and _text_width(fit, font, size) > CONTENT_W - 8:
                    fit = fit[:-1]
                self._page().text(MARGIN + 8, self.y,
                                  [(font, fit or "", size,
                                    _text_width(fit or "", font, size))])
                self.y -= leading
                text = text[len(fit):]
                if not text:
                    break
        self.space(4.0)

    # -- output --
    def to_bytes(self, *, title: str = "") -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font_ids = {}
        for name, base in _FONT_BASE.items():
            fid = add(
                f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} "
                f"/Encoding /WinAnsiEncoding >>".encode("latin-1")
            )
            font_ids[name] = fid

        content_ids: list[int] = []
        for page in self.pages:
            comp = zlib.compress(page.stream())
            cid = add(
                b"<< /Length " + str(len(comp)).encode() +
                b" /Filter /FlateDecode >>\nstream\n" + comp + b"\nendstream"
            )
            content_ids.append(cid)

        real_page_ids = []
        for idx, cid in enumerate(content_ids):
            fonts = " ".join(f"/{n} {font_ids[n]} 0 R" for n in _FONT_BASE)
            pid = add(
                (f"<< /Type /Page /Parent __PAGES__ 0 R "
                 f"/MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
                 f"/Resources << /Font << {fonts} >> >> "
                 f"/Contents {cid} 0 R >>").encode("latin-1")
            )
            real_page_ids.append(pid)

        kids = " ".join(f"{pid} 0 R" for pid in real_page_ids)
        pages_obj = (f"<< /Type /Pages /Count {len(real_page_ids)} "
                     f"/Kids [{kids}] >>").encode("latin-1")
        pages_id = add(pages_obj)

        # patch the __PAGES__ placeholder in the page objects
        for pid in real_page_ids:
            objects[pid - 1] = objects[pid - 1].replace(
                b"__PAGES__", str(pages_id).encode())

        info_id = add(
            ("<< /Title (" + _pdf_escape(_ascii(title or "Document")) +
             ") /Producer (revenue_os.pdf) >>").encode("latin-1")
        )
        catalog_id = add(
            f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1")
        )

        # assemble
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for i, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
        xref_pos = len(out)
        n = len(objects) + 1
        out += f"xref\n0 {n}\n".encode("latin-1")
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode("latin-1")
        out += (f"trailer\n<< /Size {n} /Root {catalog_id} 0 R "
                f"/Info {info_id} 0 R >>\nstartxref\n{xref_pos}\n"
                f"%%EOF\n").encode("latin-1")
        return bytes(out)


# --- Markdown -> PDF -------------------------------------------------

_H = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_NUM = re.compile(r"^(\d+)\.\s+(.*)$")
_FENCE = re.compile(r"^\s*```")


def render_markdown_pdf(md: str, *, title: str = "") -> bytes:
    doc = PdfDoc()
    lines = str(md).replace("\r\n", "\n").split("\n")
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            text = " ".join(s.strip() for s in para)
            if text.startswith("_") and text.endswith("_") and len(text) > 2:
                doc.paragraph(text[1:-1], "ital")
            else:
                doc.paragraph(text, "body")
            doc.space(5.0)
            para = []

    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            flush_para()
            block: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                block.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            doc.code_block(block)
            continue

        stripped = line.strip()
        if not stripped:
            flush_para()
            i += 1
            continue

        m = _H.match(stripped)
        if m:
            flush_para()
            doc.heading(m.group(2).strip(), len(m.group(1)))
            i += 1
            continue

        if stripped in ("---", "***", "___"):
            flush_para()
            doc.rule()
            i += 1
            continue

        mb = _BULLET.match(stripped)
        if mb:
            flush_para()
            item = mb.group(1).strip()
            if item.startswith("[ ] "):
                doc.paragraph(item[4:], "body", indent=18, bullet="[ ]")
            elif item.startswith("[x] ") or item.startswith("[X] "):
                doc.paragraph(item[4:], "body", indent=18, bullet="[x]")
            else:
                doc.paragraph(item, "body", indent=18, bullet="-")
            i += 1
            continue

        mn = _NUM.match(stripped)
        if mn:
            flush_para()
            doc.paragraph(mn.group(2).strip(), "body", indent=22,
                          bullet=mn.group(1) + ".")
            i += 1
            continue

        para.append(line)
        i += 1

    flush_para()
    return doc.to_bytes(title=title)
