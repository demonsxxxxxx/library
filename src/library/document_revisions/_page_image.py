"""Render a physical page in its own PDFium process (PDFium is not thread-safe)."""
import io
import sys

import pypdfium2 as pdfium


def main():
    with pdfium.PdfDocument(sys.stdin.buffer.read()) as doc:
        n = int(sys.argv[1])
        if not 1 <= n <= len(doc):
            raise ValueError("no such physical page")
        page = doc[n - 1]
        try:
            bitmap = page.render(scale=1400 / max(page.get_size()))
            try:
                image = bitmap.to_pil().convert("RGB")
                out = io.BytesIO()
                image.save(out, "JPEG", quality=88)
                sys.stdout.buffer.write(out.getvalue())
            finally:
                bitmap.close()
        finally:
            page.close()


if __name__ == "__main__":
    main()
