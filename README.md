# ppt_to_notes

CLI to turn PPT/PPTX decks into LaTeX notes with Gemini Vision.

## Prerequisites
- Python 3.11+
- System tools: LibreOffice (`soffice` on PATH, or macOS `/Applications/LibreOffice.app/.../soffice`), poppler for `pdf2image` (`brew install poppler` on macOS), `xelatex` (for PDF compile; optional if you skip compile).
- Python deps: `pip install -r requirements.txt`
- Gemini API key in `GEMINI_API_KEY` or `GOOGLE_API_KEY` (or pass `--api-key`). You can also set `GEN_AI_API_KEYS` (comma-separated; first key is used). Install `google-genai` if you want live calls; otherwise placeholders are written.

## Commands
```
# One-shot
python ppt2tex.py convert input.pptx --out out --dpi 300 --model gemini-2.5-pro

# Debug stages
python ppt2tex.py ingest input.pptx --out out --dpi 300
python ppt2tex.py extract --slides out/slides --out out/fragments --model gemini-2.5-pro
python ppt2tex.py compose --fragments out/fragments --out-tex out/document.tex
```
`convert` optionally runs `xelatex` in `out/`; use `--no-compile-pdf` to skip.

## Output layout
```
out/
  slides/slide_0001.png
  fragments/slide_0001.tex
  pages/slide_0001.txt    # "Page : 1" + Gemini OCR text per page
  template.tex   # auto-created if missing
  document.tex   # stitched via Jinja2
  document.pdf   # when xelatex is available
```

## Streamlit UI (upload/drag-drop)
```
streamlit run streamlit_app.py
```
Drag/drop or upload a .ppt/.pptx, choose a Gemini model from the dropdown (or custom), optionally set DPI/API key, and download PDF/LaTeX/zip outputs. The UI shows live step status (ingest → extract → compose → optional compile). If you leave the API key blank, placeholder fragments are written. Per-page `.txt` files (Gemini OCR text) live in `out/pages/`.

## Notes
- Vision prompt mirrors the spec in `context.txt`; each slide is processed independently and will never block the pipeline. On failures, a TODO block with the slide image is written.
- CLI prints every Gemini response (LaTeX and OCR) to stdout for visibility.
- Template is minimal; edit `out/template.tex` to tweak styling. Slides are referenced relative to `out/` (`slides/<file>.png`).
