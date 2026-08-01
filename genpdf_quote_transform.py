import html
import re
import sys
from pathlib import Path

RTL_CHAR_PATTERN = re.compile(r"[\u0590-\u08FF\uFB1D-\uFDFD\uFE70-\uFEFC]")


def _is_rtl_text(text: str) -> bool:
    return bool(RTL_CHAR_PATTERN.search(text))


def transform_markdown_quotes(src_path: Path | str, out_path: Path | str) -> None:
    src = Path(src_path)
    out = Path(out_path)

    lines = src.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(">"):
            quote_lines: list[str] = []
            while i < len(lines) and lines[i].startswith(">"):
                quote_lines.append(lines[i][1:].lstrip(" "))
                i += 1
            content = "\n".join(part.strip() for part in quote_lines if part.strip())
            if content:
                paragraphs = [part.strip() for part in content.splitlines() if part.strip()]
                if any(_is_rtl_text(paragraph) for paragraph in paragraphs):
                    wrapped = "\n".join(
                        f'<p dir="rtl" lang="ar">{html.escape(paragraph)}</p>'
                        for paragraph in paragraphs
                    )
                    out_lines.append('<blockquote class="report-quote" dir="rtl" lang="ar">')
                else:
                    wrapped = "\n".join(
                        f'<p dir="ltr" lang="en">{html.escape(paragraph)}</p>'
                        for paragraph in paragraphs
                    )
                    out_lines.append('<blockquote class="report-quote" dir="ltr" lang="en">')
                out_lines.append('<div class="report-quote-body">')
                out_lines.append(wrapped)
                out_lines.append("</div>")
                out_lines.append("</blockquote>")
                continue
        out_lines.append(line)
        i += 1

    out.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: genpdf_quote_transform.py <input.md> <output.md>")
    transform_markdown_quotes(sys.argv[1], sys.argv[2])
