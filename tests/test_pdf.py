import re
import unittest
import zlib

from revenue_os.pdf import PdfDoc, render_markdown_pdf

_SAMPLE = """# Customer Launch Plan

Prepared for **Jane Doe** (Acme Widgets).

_This is a personalised research and strategy document._

## 1. Business & product analysis

- **What you sell:** hand-made widgets
- **Problem it solves:** slow widget procurement

### Detail

Some longer paragraph text that is definitely wide enough that it has to
wrap onto more than one line when rendered into the fixed A4 content
width, several times over, to exercise the greedy word-wrap path here.

## 5. 14-day action plan

1. Day 1 - set up the store
2. Day 2 - reach out to five prospects

## 6. Templates

```
Subject: quick question
Hi {name}, I saw your post about {problem}.
```

## 7. Next steps

- [ ] publish the checkout page
- [x] confirm the price

---

_Basis: web search, 4 sources._
"""


def _rendered_text(pdf: bytes) -> str:
    """Concatenate every ( ... ) Tj literal across all content streams,
    unescaping the PDF string escapes, so tests can assert on visible text."""
    streams = re.findall(rb"stream\n(.*?)\nendstream", pdf, re.S)
    raw = b"".join(zlib.decompress(s) for s in streams).decode("latin-1")
    out = []
    for m in re.finditer(r"\((.*?)(?<!\\)\)\s*Tj", raw, re.S):
        s = m.group(1)
        s = s.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
        out.append(s)
    return "".join(out)


class PdfStructureTests(unittest.TestCase):
    def test_valid_pdf_envelope(self):
        pdf = render_markdown_pdf(_SAMPLE, title="Customer Launch Plan")
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/Type /Catalog", pdf)
        self.assertIn(b"/Type /Pages", pdf)
        self.assertIn(b"/Type /Page ", pdf)
        self.assertIn(b"startxref", pdf)

    def test_xref_offsets_point_at_objects(self):
        pdf = render_markdown_pdf(_SAMPLE)
        m = re.search(rb"startxref\n(\d+)\n%%EOF", pdf)
        self.assertIsNotNone(m)
        xref_start = int(m.group(1))
        xref = pdf[xref_start:xref_start + 6]
        self.assertEqual(xref, b"xref\n0")
        # every "N 0 obj" actually begins where an xref entry says it does
        entries = re.findall(rb"^(\d{10}) 00000 n $", pdf[xref_start:], re.M)
        self.assertGreater(len(entries), 4)
        for off in entries:
            pos = int(off)
            self.assertRegex(pdf[pos:pos + 12], rb"^\d+ 0 obj")

    def test_content_streams_decompress(self):
        pdf = render_markdown_pdf(_SAMPLE)
        text = _rendered_text(pdf)
        self.assertIn("Customer Launch Plan", text)
        self.assertIn("hand-made widgets", text)
        self.assertIn("publish the checkout page", text)
        self.assertIn("Hi {name}, I saw your post about {problem}.", text)

    def test_page_count_grows_with_content(self):
        one = render_markdown_pdf("short line")
        many = render_markdown_pdf("\n\n".join(["paragraph %d" % i for i in range(400)]))
        n_one = len(re.findall(rb"/Type /Page ", one))
        n_many = len(re.findall(rb"/Type /Page ", many))
        self.assertEqual(n_one, 1)
        self.assertGreater(n_many, 1)
        # /Count matches the number of page objects
        count = int(re.search(rb"/Type /Pages /Count (\d+)", many).group(1))
        self.assertEqual(count, n_many)

    def test_parens_and_unicode_are_safe(self):
        pdf = render_markdown_pdf("Price is 29.90 EUR (with a smart quote ’ and dash —).")
        # raw stream keeps the ( escaped so it cannot break the string operator
        raw = b"".join(
            zlib.decompress(s)
            for s in re.findall(rb"stream\n(.*?)\nendstream", pdf, re.S)
        )
        self.assertIn(rb"\(with", raw)
        self.assertNotIn("’".encode("utf-8"), raw)   # smart quote transliterated
        text = _rendered_text(pdf)
        self.assertIn("(with a smart quote ' and dash -)", text)

    def test_empty_input_still_valid(self):
        pdf = render_markdown_pdf("")
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertIn(b"/Type /Page ", pdf)

    def test_direct_doc_api(self):
        doc = PdfDoc()
        doc.heading("Title", 1)
        doc.paragraph("Body text.", "body")
        doc.paragraph("bullet", "body", indent=18, bullet="-")
        out = doc.to_bytes(title="t")
        self.assertTrue(out.startswith(b"%PDF-1.4"))


if __name__ == "__main__":
    unittest.main()
