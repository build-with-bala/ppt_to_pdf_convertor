"""CLI to convert PowerPoint slides into LaTeX notes using Gemini Vision."""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional

import typer
from jinja2 import Template
from pdf2image import convert_from_path
from pydantic import BaseModel, Field

try:  # Optional; we degrade gracefully if the SDK is missing.
    from google import genai
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover - optional dependency
    genai = None
    genai_types = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover - optional dependency
    Image = None

app = typer.Typer(help="Convert PPT/PPTX files to LaTeX notes via Gemini Vision.")

SOFFICE_MAC = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
DEFAULT_MODEL = "gemini-2.5-pro"
DEFAULT_TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage{fontspec}
\setmainfont{Times New Roman}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{hyperref}
\usepackage[noabbrev]{cleveref}
\hypersetup{colorlinks=true, linkcolor=blue, urlcolor=blue}
\title{Auto Notes} \date{\today}
\begin{document}
\maketitle
\tableofcontents
% === SLIDE CONTENT START ===
{{ content }}
% === SLIDE CONTENT END ===
\end{document}
"""

VISION_PROMPT = """You are converting a lecture slide IMAGE to LaTeX.

Return ONLY valid LaTeX that can be pasted inside a document body:
- Use \\section{{}} or \\subsection{{}} if the slide has a clear title.
- Inline math: $...$, display math: \\[...\\].
- Figures: if needed, include:
  \\begin{{figure}}\\centering
  \\includegraphics[width=\\linewidth]{{{slide_rel_path}}}
  \\caption{{From slide {slide_number}}}
  \\label{{fig:s{slide_number}-1}}
  \\end{{figure}}
No explanations, no markdown, just LaTeX."""

OCR_PROMPT = """You are an OCR engine. Transcribe ALL visible text from the slide image.
- Return plain text only (no Markdown, no LaTeX, no explanations).
- Keep reading order top-to-bottom/left-to-right; preserve math symbols as-is.
- Include bullet prefixes or numbering only if they visibly appear."""

FENCE_RE = re.compile(r"```(?:latex)?|```", re.IGNORECASE)


class SlideFragment(BaseModel):
    """Structured container for a single slide's LaTeX fragment."""

    slide: str
    latex: str
    warnings: List[str] = Field(default_factory=list)
    raw: Optional[str] = None

    def render(self) -> str:
        if self.warnings:
            warn = "% " + " | ".join(self.warnings)
            return warn + "\n" + self.latex
        return self.latex


def run(cmd: List[str], cwd: Optional[Path] = None) -> None:
    """Run a shell command and bubble up failures."""
    subprocess.run(cmd, cwd=cwd, check=True)


def find_soffice() -> Path:
    """Best-effort discovery for the LibreOffice soffice binary."""
    candidates = [
        SOFFICE_MAC,
        shutil.which("soffice"),
        shutil.which("libreoffice"),
    ]
    for cand in candidates:
        if not cand:
            continue
        path = Path(cand)
        if path.exists():
            return path
    raise FileNotFoundError(
        "LibreOffice soffice not found. Install LibreOffice or ensure 'soffice' is on PATH."
    )


def to_pdf(ppt: Path, outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    soffice = find_soffice()
    run(
        [
            str(soffice),
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(outdir),
            str(ppt),
        ]
    )
    pdf_path = outdir / f"{ppt.stem}.pdf"
    if not pdf_path.exists():
        raise RuntimeError(f"Conversion to PDF failed; {pdf_path} not found.")
    return pdf_path


def pdf_to_pngs(pdf: Path, slides_dir: Path, dpi: int = 300) -> List[Path]:
    slides_dir.mkdir(parents=True, exist_ok=True)
    pages = convert_from_path(str(pdf), dpi=dpi)
    paths: List[Path] = []
    for i, page in enumerate(pages, start=1):
        path = slides_dir / f"slide_{i:04d}.png"
        page.save(path)
        paths.append(path)
    return paths


def slide_number_from_name(stem: str) -> str:
    match = re.search(r"(\d+)$", stem)
    if not match:
        return stem
    stripped = match.group(1).lstrip("0")
    return stripped or match.group(1)


def sanitize_latex(raw: str) -> tuple[str, List[str]]:
    """Strip markdown fences and add minimal balance fixes."""
    warnings: List[str] = []
    text = FENCE_RE.sub("", raw).strip()
    if not text:
        return "", ["Empty response from model"]

    dollar_count = len(re.findall(r"(?<!\\)\$", text))
    if dollar_count % 2 != 0:
        text += " $"
        warnings.append("Balanced dangling $ with a closing $.")

    bracket_diff = text.count("\\[") - text.count("\\]")
    if bracket_diff > 0:
        text += " " + " ".join("\\]" for _ in range(bracket_diff))
        warnings.append("Closed unmatched \\[ delimiters.")
    elif bracket_diff < 0:
        warnings.append("More \\] than \\[; please review.")

    return text, warnings


def strip_fences(raw: str) -> str:
    """Remove markdown fences and trim."""
    return FENCE_RE.sub("", raw).strip()


def _read_image_bytes(img_path: Path) -> bytes:
    """Return PNG bytes for Gemini Vision."""
    with Image.open(img_path) as img:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def _image_part(img_path: Path):
    """Return a genai Part for an image."""
    if genai_types is None:
        return None
    return genai_types.Part.from_bytes(data=_read_image_bytes(img_path), mime_type="image/png")


def load_api_keys(explicit_key: Optional[str]) -> List[str]:
    """Load API keys from explicit arg, GEN_AI_API_KEYS, GEMINI_API_KEY/GOOGLE_API_KEY."""
    keys: List[str] = []
    if explicit_key:
        keys.append(explicit_key)
    env_keys = [k.strip() for k in os.environ.get("GEN_AI_API_KEYS", "").split(",") if k.strip()]
    keys.extend(env_keys)
    legacy = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if legacy:
        keys.append(legacy)

    seen = set()
    deduped = []
    for key in keys:
        if key and key not in seen:
            deduped.append(key)
            seen.add(key)
    return deduped


def generate_with_retry(
    prompt: str,
    img_path: Optional[Path],
    model_name: str,
    api_key: Optional[str],
    tag: str,
) -> tuple[Optional[str], Optional[str]]:
    """Gemini call with a single 429 retry (60s wait). Returns (text, error)."""
    if genai is None or Image is None or genai_types is None:
        return None, "google-genai or pillow not installed (pip install google-genai pillow)."

    keys = load_api_keys(api_key)
    if not keys:
        return None, "Missing API key(s). Set GEN_AI_API_KEYS or GEMINI_API_KEY/GOOGLE_API_KEY."

    key = keys[0]
    attempts = 2  # initial + one retry for 429
    for attempt in range(attempts):
        try:
            client = genai.Client(api_key=key)
            contents = [prompt]
            if img_path is not None:
                part = _image_part(img_path)
                if part is None:
                    return None, "genai types unavailable for image part."
                contents.append(part)
            response = client.models.generate_content(model=model_name, contents=contents)
            text = response.text or ""
            print(f"[Gemini {tag}][{img_path.name if img_path else 'text'}][key-0] {text}")
            return text, None
        except Exception as exc:  # pragma: no cover - defensive catch
            is_429 = "429" in str(exc) or "Resource has been exhausted" in str(exc)
            if is_429 and attempt == 0:
                time.sleep(60)
                continue
            return None, f"{tag} failed: {exc}"


def placeholder_fragment(img_path: Path, slides_root: Path, reason: str) -> SlideFragment:
    slide_number = slide_number_from_name(img_path.stem)
    rel_path = str(img_path.relative_to(slides_root.parent))
    latex = rf"""\section*{{Slide {slide_number}}}
% TODO: {reason}
\begin{{figure}}[h]
\centering
\includegraphics[width=\linewidth]{{{rel_path}}}
\caption{{From slide {slide_number}}}
\label{{fig:s{slide_number}-todo}}
\end{{figure}}
"""
    return SlideFragment(slide=img_path.stem, latex=latex, warnings=[reason], raw=None)


def build_prompt(img_path: Path, slides_root: Path) -> str:
    slide_number = slide_number_from_name(img_path.stem)
    rel_path = str(img_path.relative_to(slides_root.parent))
    return VISION_PROMPT.format(slide_rel_path=rel_path, slide_number=slide_number)


def call_gemini(
    img_path: Path,
    slides_root: Path,
    model_name: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
) -> SlideFragment:
    prompt = build_prompt(img_path, slides_root)
    text, error = generate_with_retry(prompt, img_path, model_name, api_key, tag="LaTeX")
    if error:
        return placeholder_fragment(img_path, slides_root, error)
    latex, warnings = sanitize_latex(text or "")
    if not latex:
        return placeholder_fragment(img_path, slides_root, "; ".join(warnings))
    return SlideFragment(slide=img_path.stem, latex=latex, warnings=warnings, raw=text)


def call_gemini_ocr_text(
    img_path: Path,
    model_name: str = DEFAULT_MODEL,
    api_key: Optional[str] = None,
) -> str:
    """Plain-text OCR via Gemini Vision; returns best-effort string."""
    text, error = generate_with_retry(OCR_PROMPT, img_path, model_name, api_key, tag="OCR")
    if error:
        return f"TODO: {error}"
    text = strip_fences(text or "")
    print(f"[Gemini OCR][{img_path.name}] {text}")
    return text or "TODO: Empty OCR response from model."


@app.command()
def ingest(
    input_ppt: Path = typer.Argument(..., help="Input .ppt/.pptx file"),
    out: Path = typer.Option(Path("out"), help="Output directory root"),
    dpi: int = typer.Option(300, help="DPI for rendered slide PNGs"),
):
    """Convert PPT/PPTX to PDF, then render PNGs."""
    pdf = to_pdf(input_ppt, out)
    pdf_to_pngs(pdf, out / "slides", dpi)
    typer.echo("Ingest done.")


@app.command()
def extract(
    slides: Path = typer.Option(Path("out/slides"), help="Folder of slide PNGs"),
    out: Path = typer.Option(Path("out/fragments"), help="Folder for LaTeX fragments"),
    pages_out: Path = typer.Option(
        Path("out/pages"), help="Folder for plain text per-page outputs"
    ),
    model: str = typer.Option(DEFAULT_MODEL, help="Gemini model name"),
    api_key: Optional[str] = typer.Option(
        None,
        help="Gemini API key (defaults to GEMINI_API_KEY or GOOGLE_API_KEY env vars).",
    ),
):
    """Send each slide image to Gemini Vision and save LaTeX fragments + OCR text."""
    out.mkdir(parents=True, exist_ok=True)
    pages_out.mkdir(parents=True, exist_ok=True)
    slides_root = slides
    for img in sorted(slides_root.glob("slide_*.png")):
        fragment = call_gemini(img, slides_root, model, api_key)
        slide_number = slide_number_from_name(img.stem)
        (out / f"{img.stem}.tex").write_text(fragment.render())
        ocr_text = call_gemini_ocr_text(img, model, api_key)
        page_text = f"Page : {slide_number}\n{ocr_text}"
        (pages_out / f"{img.stem}.txt").write_text(page_text)
    typer.echo("Extract done.")


@app.command()
def compose(
    fragments: Path = typer.Option(Path("out/fragments"), help="Fragment folder"),
    template: Path = typer.Option(Path("out/template.tex"), help="LaTeX template path"),
    out_tex: Path = typer.Option(Path("out/document.tex"), help="Rendered .tex output"),
):
    """Stitch LaTeX fragments into a single document.tex."""
    if not template.exists():
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(DEFAULT_TEMPLATE)

    content = "\n\n".join(
        (fragments / f).read_text() for f in sorted(p.name for p in fragments.glob("*.tex"))
    )
    tpl = Template(template.read_text())
    out_tex.parent.mkdir(parents=True, exist_ok=True)
    out_tex.write_text(tpl.render(content=content))
    typer.echo(f"Wrote {out_tex}. Compile with: xelatex {out_tex.name}")


@app.command()
def convert(
    input_ppt: Path = typer.Argument(..., help="Input .ppt/.pptx file"),
    out: Path = typer.Option(Path("out"), help="Output directory"),
    dpi: int = typer.Option(300, help="DPI for rendered slide PNGs"),
    model: str = typer.Option(DEFAULT_MODEL, help="Gemini model name"),
    api_key: Optional[str] = typer.Option(
        None,
        help="Gemini API key (defaults to GEMINI_API_KEY or GOOGLE_API_KEY env vars).",
    ),
    compile_pdf: bool = typer.Option(True, help="Run xelatex after composing"),
):
    """Full pipeline: ingest -> extract -> compose -> (optional) compile."""
    ingest(input_ppt, out, dpi)
    extract(out / "slides", out / "fragments", out / "pages", model, api_key)
    compose(out / "fragments", out / "template.tex", out / "document.tex")
    if compile_pdf:
        try:
            run(["xelatex", "document.tex"], cwd=out)
            typer.echo("Compile done.")
        except FileNotFoundError:
            typer.echo("xelatex not found on PATH; skipping compile.", err=True)
        except subprocess.CalledProcessError as exc:
            typer.echo(f"xelatex failed: {exc}", err=True)
    typer.echo("Done.")


if __name__ == "__main__":
    app()
