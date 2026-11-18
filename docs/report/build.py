"""Build the Project Work-I report PDF.

    python3 docs/report/build.py            (needs Google Chrome and PyMuPDF: pip install pymupdf)

1. report_src.html -> report.html: figures inlined, references, appendix,
   lists of figures/tables and the table of contents generated.
2. Chrome (headless) prints report.html to PDF.
3. PyMuPDF finds the page of every heading and caption, the contents and
   lists are filled with real page numbers, the PDF is printed again, and
   page numbers are stamped (none on the title page, roman for the front
   matter, arabic from Chapter 1).

Diagrams: diagrams/*.mmd are rendered to figs/*.svg separately (Mermaid in a
headless browser); fig3_1 is drawn by diagrams/fig3_1_architecture.py.
"""

from __future__ import annotations

import base64
import html
import os
import re
import subprocess
import sys

import fitz  # PyMuPDF

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "report_src.html")
OUT_HTML = os.path.join(HERE, "report.html")
OUT_PDF = os.path.join(os.path.dirname(HERE), "ProjectWork-I_Report.pdf")
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def roman(n: int) -> str:
    vals = [(10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out


def inline_svg(path: str, width_pct: int) -> str:
    svg = open(os.path.join(HERE, path)).read()
    svg = svg.strip()
    # size by the column width; keep the aspect ratio from the viewBox.
    # Only the opening <svg> tag is touched: inner elements keep their sizes.
    end = svg.index(">") + 1
    tag, rest = svg[:end], svg[end:]
    tag = re.sub(r'\s(width|height)="[^"]*"', "", tag)
    tag = re.sub(r'\sstyle="[^"]*"', "", tag)
    svg = tag + rest
    # a small margin so edge boxes (e.g. the last sequence participant) are not clipped
    def pad(m):
        x, y, w, h = (float(v) for v in m.group(1).split())
        return f'viewBox="{x-12} {y-12} {w+24} {h+24}"'
    svg = re.sub(r'viewBox="([^"]+)"', pad, svg, count=1)
    return svg.replace("<svg", f'<svg style="width:{width_pct}%;height:auto"', 1)


def data_uri(path: str) -> str:
    raw = open(os.path.join(HERE, path), "rb").read()
    mime = "image/png" if path.endswith(".png") else "image/svg+xml"
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def build_html(pages: dict) -> str:
    s = open(SRC).read()
    figs, tabs, heads = [], [], []

    def fig(m, kind):
        path, width, cap = m.group(1), int(m.group(2)), m.group(3)
        num = re.match(r"Fig\. ([\d.]+):", cap).group(1)
        fid = "fig-" + num.replace(".", "-")
        figs.append((num, cap.split(": ", 1)[1], fid))
        body = inline_svg(path, width) if kind == "FIG" else \
            f'<img src="{data_uri(path)}" style="width:{width}%;border:0.5pt solid #999">'
        return f'<div class="fig">{body}</div><p class="fcap" id="{fid}">{html.escape(cap)}</p>'

    s = re.sub(r"\{\{FIG:([^:]+):(\d+):([^}]+)\}\}", lambda m: fig(m, "FIG"), s)
    s = re.sub(r"\{\{IMG:([^:]+):(\d+):([^}]+)\}\}", lambda m: fig(m, "IMG"), s)
    s = re.sub(r"\{\{ASSET:([^}]+)\}\}", lambda m: data_uri(m.group(1)), s)

    def tcap(m):
        cap = m.group(1)
        num = re.match(r"Table ([\d.]+):", cap).group(1)
        tid = "tab-" + num.replace(".", "-")
        tabs.append((num, cap.split(": ", 1)[1], tid))
        return f'<p class="tcap" id="{tid}">'

    s = re.sub(r'<p class="tcap" data-table="([^"]+)">', tcap, s)

    for m in re.finditer(r'<h([123]) class="[^"]*" id="([^"]+)" data-toc="([^"]+)"(?: data-lvl="(\d)")?|'
                         r'<h([23]) id="([^"]+)" data-toc="([^"]+)"(?: data-lvl="(\d)")?', s):
        if m.group(2):
            hid, text, lvl = m.group(2), m.group(3), int(m.group(4) or 1)
        else:
            hid, text, lvl = m.group(6), m.group(7), int(m.group(8) or 2)
        heads.append((lvl, text, hid))

    def pg(key):
        return pages.get(key, "00")

    def dotted(text, key):
        return f'<div class="dots"><span class="t">{html.escape(text)}</span><span class="d"></span></div>'

    toc = ['<table class="toc"><tr class="lvl1"><td><b>Contents</b></td><td class="p"><b>Page No.</b></td></tr>']
    for text, key in (("Certificate", "fm-certificate"), ("Abstract", "fm-abstract"), ("List of Figures", "fm-lof"),
                      ("List of Tables", "fm-lot"), ("List of Abbreviations", "fm-loa"), ("Table of Contents", "fm-toc")):
        toc.append(f'<tr><td>{dotted(text, key)}</td><td class="p">{pg(key)}</td></tr>')
    for lvl, text, hid in heads:
        toc.append(f'<tr class="lvl{lvl}"><td>{dotted(text, hid)}</td><td class="p">{pg(hid)}</td></tr>')
    toc.append("</table>")
    s = s.replace("{{TOC}}", "\n".join(toc))

    def lst(rows, head):
        out = [f'<table class="lst"><tr><th style="width:15%">{head} No.</th><th>Name of the {"Figure" if head == "Fig." else "Table"}</th>'
               '<th style="width:14%">Page No.</th></tr>']
        for num, name, rid in rows:
            out.append(f'<tr><td class="n">{num}</td><td>{html.escape(name)}</td><td class="p">{pg(rid)}</td></tr>')
        return "\n".join(out) + "</table>"

    key = lambda r: tuple(int(x) for x in r[0].split("."))
    figs.sort(key=key)
    tabs.sort(key=key)
    s = s.replace("{{LOF}}", lst(figs, "Fig.")).replace("{{LOT}}", lst(tabs, "Table"))

    refs = [r.strip() for r in open(os.path.join(HERE, "references.txt")) if r.strip()]
    s = s.replace("{{REFS}}", "\n".join(
        f'<tr><td style="border:none;width:9mm;vertical-align:top">[{i}]</td>'
        f'<td style="border:none;text-align:justify;padding-bottom:4pt">{html.escape(r)}</td></tr>'
        for i, r in enumerate(refs, 1)))

    order = [("fig3_2_closed_loop", "Fig. 3.2 Closed-Loop Workflow"), ("fig3_3_deployment", "Fig. 3.3 Live Lab Deployment"),
             ("fig3_4_modules", "Fig. 3.4 Module Architecture"), ("fig3_10_class", "Fig. 3.12 Class Diagram"),
             ("fig3_11_component", "Fig. 3.13 Component Diagram"), ("fig3_12_usecase", "Fig. 3.14 Use Case Diagram"),
             ("fig3_13_activity", "Fig. 3.15 Activity Diagram"), ("fig3_14_sequence", "Fig. 3.16 Sequence Diagram")]
    app = []
    for i, (name, title) in enumerate(order, 1):
        src = open(os.path.join(HERE, "diagrams", f"{name}.mmd")).read().rstrip()
        app.append(f"<h3>A.{i} {html.escape(title)}</h3><pre class=\"src\">{html.escape(src)}</pre>")
    s = s.replace("{{APPENDIX}}", "\n".join(app))
    return s, figs, tabs, heads


def print_pdf(html_path: str, pdf_path: str) -> None:
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=5000",
                    f"--print-to-pdf={pdf_path}", f"file://{html_path}"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def locate(pdf_path: str, figs, tabs, heads) -> dict:
    doc = fitz.open(pdf_path)
    texts = [p.get_text() for p in doc]
    norm = [re.sub(r"\s+", " ", t) for t in texts]

    def find(needle, start=0):
        n = re.sub(r"\s+", " ", needle)
        for i in range(start, len(norm)):
            if n in norm[i]:
                return i
        return None

    toc_page = find("TABLE OF CONTENTS")
    ch1 = find("CHAPTER 1 INTRODUCTION", toc_page + 1)
    loc = {}
    for key, needle in (("fm-certificate", "CERTIFICATE"), ("fm-abstract", "ABSTRACT"), ("fm-lof", "LIST OF FIGURES"),
                        ("fm-lot", "LIST OF TABLES"), ("fm-loa", "LIST OF ABBREVIATIONS"), ("fm-toc", "TABLE OF CONTENTS")):
        loc[key] = find(needle)
    search_from = ch1
    for lvl, text, hid in heads:
        needle = {"ch1": "CHAPTER 1 INTRODUCTION", "ch2": "CHAPTER 2 LITERATURE SURVEY", "ch3": "CHAPTER 3 SYSTEM DESIGN",
                  "ch4": "CHAPTER 4 IMPLEMENTATION STATUS", "refs": "REFERENCES [1]",
                  "appA": "APPENDIX A – MERMAID SOURCE OF DIAGRAMS The"}.get(hid, text)
        i = find(needle, search_from)
        if i is None:
            i = find(text.split(" ", 1)[-1], search_from)
        loc[hid] = i
        if i is not None:
            search_from = i
    for num, name, rid in figs:
        loc[rid] = find(f"Fig. {num}: {name}", ch1)
    for num, name, rid in tabs:
        loc[rid] = find(f"Table {num}: {name}", ch1)
    missing = [k for k, v in loc.items() if v is None]
    if missing:
        print("WARNING: not located:", missing)
    label = {k: (roman(v) if v < ch1 else str(v - ch1 + 1)) for k, v in loc.items() if v is not None}
    return label, ch1, len(texts)


def stamp(pdf_path: str, ch1: int) -> None:
    doc = fitz.open(pdf_path)
    for i, page in enumerate(doc):
        if i == 0:
            continue
        label = roman(i) if i < ch1 else str(i - ch1 + 1)
        r = page.rect
        page.insert_textbox(fitz.Rect(0, r.height - 50, r.width, r.height - 30), label, fontsize=11,
                            fontname="Times-Roman", align=fitz.TEXT_ALIGN_CENTER)
    doc.set_metadata({"title": "AI-Powered Zero Trust Self-Healing Enterprise Network - Project Work-I Report",
                      "author": "P. Vamshi Vikram, P. Amitesh, G. Jeeyar", "subject": "Project Work-I Report",
                      "creator": "docs/report/build.py"})
    tmp = pdf_path + ".tmp"
    doc.save(tmp, garbage=3, deflate=True)
    doc.close()
    os.replace(tmp, pdf_path)


def main() -> int:
    html1, figs, tabs, heads = build_html({})
    open(OUT_HTML, "w").write(html1)
    print_pdf(OUT_HTML, OUT_PDF)
    pages, ch1, n = locate(OUT_PDF, figs, tabs, heads)
    html2, *_ = build_html(pages)
    open(OUT_HTML, "w").write(html2)
    print_pdf(OUT_HTML, OUT_PDF)
    pages2, ch1b, n2 = locate(OUT_PDF, figs, tabs, heads)
    if pages2 != pages:  # page numbers shifted the layout: one more pass
        html3, *_ = build_html(pages2)
        open(OUT_HTML, "w").write(html3)
        print_pdf(OUT_HTML, OUT_PDF)
        pages2, ch1b, n2 = locate(OUT_PDF, figs, tabs, heads)
    stamp(OUT_PDF, ch1b)
    print(f"{OUT_PDF}: {n2} pages, chapter 1 starts on sheet {ch1b + 1}; "
          f"{len(figs)} figures, {len(tabs)} tables, {len(heads)} headings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
