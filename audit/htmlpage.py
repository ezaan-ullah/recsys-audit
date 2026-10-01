"""Small helpers for the self-contained local HTML pages (labeling, seed review, report)."""
import html
import re

esc = html.escape

# Shared page chrome: light/dark tokens, system sans, 16px gutters.
BASE_CSS = """
:root { color-scheme: light; --bg:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --line:#e1e0d9; --accent:#2a78d6; --accent2:#eb6834; --warn:#d03b3b; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark; --bg:#0d0d0d;
  --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781; --line:#2c2c2a; --accent:#3987e5; --accent2:#d95926; --warn:#e66767; } }
:root[data-theme="dark"] { color-scheme: dark; --bg:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7;
  --muted:#898781; --line:#2c2c2a; --accent:#3987e5; --accent2:#d95926; --warn:#e66767; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 19px; margin: 32px 0 8px; } h3 { font-size: 16px; margin: 20px 0 6px; }
p, li { color: var(--ink2); } code { font-size: 13px; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 16px; margin: 12px 0; }
table { border-collapse: collapse; width: 100%; font-size: 13px; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }
th { color: var(--ink2); font-weight: 600; }
.scroll { overflow-x: auto; }
.muted { color: var(--muted); }
img.blur { filter: blur(22px) grayscale(1); cursor: pointer; transition: filter .15s; }
img.blur.shown { filter: none; }
button { font: inherit; padding: 6px 14px; border-radius: 8px; border: 1px solid var(--line); background: var(--surface); color: var(--ink); cursor: pointer; }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
"""


def md_to_html(md: str) -> str:
    """Enough Markdown for the codebook: headings, paragraphs, bullet lists, tables, `code`, **bold**."""
    def inline(s: str) -> str:
        s = esc(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)

    out, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line:
            i += 1
        elif line.startswith("#"):
            level = min(len(line) - len(line.lstrip("#")) + 1, 4)
            out.append(f"<h{level}>{inline(line.lstrip('#').strip())}</h{level}>")
            i += 1
        elif line.startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, body = block[0], [r for r in block[1:] if not set("".join(r)) <= set("-: ")]
            out.append('<div class="scroll"><table><tr>' + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</table></div>")
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{inline(lines[i][2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "- ")):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
    return "\n".join(out)
