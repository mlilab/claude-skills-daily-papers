"""PDF fallback using poppler-utils (pdftotext / pdftoppm): text plus cropped figure images.

Figure cropping heuristic: find caption blocks ("Figure 3:" / "Fig. 3."), then take the
region between the caption and the nearest body-text block (or caption) above it in the
same column, and render it with pdftoppm.
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
from pathlib import Path

CAPTION_RE = re.compile(r"^(Figure|Fig\.?)\s*(\d+)\s*[:.|]\s*(.*)", re.I)
ANY_CAPTION_RE = re.compile(r"^(Figure|Fig\.?|Table)\s*\d+\s*[:.|]", re.I)
DPI = 160


def available() -> bool:
    return bool(shutil.which("pdftotext") and shutil.which("pdftoppm"))


def pdf_text(pdf: Path) -> str:
    r = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"],
                       capture_output=True, timeout=180)
    return r.stdout.decode("utf-8", "replace").replace("\f", "\n\n")


def _blocks(pdf: Path):
    r = subprocess.run(["pdftotext", "-bbox-layout", "-enc", "UTF-8", str(pdf), "-"],
                       capture_output=True, timeout=180)
    xml = r.stdout.decode("utf-8", "replace")
    pages = []
    for pm in re.finditer(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', xml, re.S):
        w, h, body = float(pm.group(1)), float(pm.group(2)), pm.group(3)
        blocks = []
        for bm in re.finditer(
                r'<block xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</block>',
                body, re.S):
            words = re.findall(r"<word [^>]*>(.*?)</word>", bm.group(5), re.S)
            text = html.unescape(" ".join(words)).strip()
            x0, y0, x1, y1 = map(float, bm.group(1, 2, 3, 4))
            blocks.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text,
                           "nwords": len(words)})
        pages.append({"w": w, "h": h, "blocks": blocks})
    return pages


def _is_boundary(b, col_w) -> bool:
    """Body paragraphs and captions bound a figure region; short labels inside plots do not."""
    if ANY_CAPTION_RE.match(b["text"]):
        return True
    return b["nwords"] >= 14 and (b["x1"] - b["x0"]) >= 0.6 * col_w


def extract_figures(pdf: Path, fig_dir: Path, max_figures: int = 40) -> list[dict]:
    fig_dir.mkdir(parents=True, exist_ok=True)
    seen, assets = set(), []
    for pno, page in enumerate(_blocks(pdf), 1):
        W, H, blocks = page["w"], page["h"], page["blocks"]
        if not blocks:
            continue
        text_top = min(b["y0"] for b in blocks)
        for cap in blocks:
            m = CAPTION_RE.match(cap["text"])
            if not m or m.group(2) in seen:
                continue
            # column span of the caption
            if cap["x1"] - cap["x0"] > 0.55 * W or (cap["x0"] < W * 0.4 and cap["x1"] > W * 0.6):
                sx0, sx1 = min(b["x0"] for b in blocks), max(b["x1"] for b in blocks)
            elif (cap["x0"] + cap["x1"]) / 2 < W / 2:
                sx0, sx1 = min(b["x0"] for b in blocks), W / 2
            else:
                sx0, sx1 = W / 2, max(b["x1"] for b in blocks)
            col_w = sx1 - sx0
            above = [b for b in blocks if b is not cap and b["y1"] <= cap["y0"] + 1
                     and b["x1"] > sx0 + 5 and b["x0"] < sx1 - 5 and _is_boundary(b, col_w)]
            top = max((b["y1"] for b in above), default=text_top - 4) + 3
            bottom = cap["y0"] - 2
            if bottom - top < 45:
                continue
            seen.add(m.group(2))
            s = DPI / 72
            x0, y0 = max(0, sx0 - 6), max(0, top)
            prefix = fig_dir / f"fig{m.group(2)}"
            subprocess.run([
                "pdftoppm", "-png", "-r", str(DPI), "-f", str(pno), "-l", str(pno),
                "-x", str(int(x0 * s)), "-y", str(int(y0 * s)),
                "-W", str(int((min(W, sx1 + 6) - x0) * s)), "-H", str(int((bottom - y0) * s)),
                "-singlefile", str(pdf), str(prefix)], capture_output=True, timeout=120)
            png = prefix.with_suffix(".png")
            if not png.exists():
                continue
            cap_text = m.group(3)
            # captions often continue in following lines of the same block only
            assets.append({
                "id": f"F{m.group(2)}", "kind": "figure", "label": f"Figure {m.group(2)}",
                "caption": cap_text, "section": f"page {pno}", "in_appendix": False,
                "files": [f"figures/{png.name}"],
            })
            if len(assets) >= max_figures:
                return assets
    return assets


def split_main_appendix(text: str) -> tuple[str, str]:
    """Cut the reference list: main = text before 'References', rest kept as appendix."""
    m = None
    for m_ in re.finditer(r"\n\s*(References|Bibliography|REFERENCES)\s*\n", text):
        if m_.start() > len(text) * 0.3:
            m = m_
            break
    if not m:
        return text, ""
    rest = text[m.end():]
    app = re.search(r"\n\s*(Appendix|APPENDIX|A\s+[A-Z][a-z]+.*)\n", rest)
    return text[:m.start()], (rest[app.start():] if app else "")
