"""Streamlit UI for ppt2tex: upload or drag/drop a PPT/PPTX and get LaTeX notes."""
from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

import subprocess

import streamlit as st

from ppt2tex import DEFAULT_MODEL, compose, extract, ingest, run


def run_pipeline(uploaded_file, dpi: int, model: str, api_key: str | None, compile_pdf: bool):
    """Write upload to a temp dir, run ppt2tex, and return downloadable bytes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_path = tmpdir_path / uploaded_file.name
        input_path.write_bytes(uploaded_file.getvalue())
        out_dir = tmpdir_path / "out"

        status = st.status("Running conversion...", expanded=True)
        try:
            status.write("Step 1/3: Ingest (PPT/PPTX → PDF/PNG)")
            ingest(input_path, out_dir, dpi)

            status.write("Step 2/3: Extract (Gemini LaTeX + OCR)")
            extract(
                slides=out_dir / "slides",
                out=out_dir / "fragments",
                pages_out=out_dir / "pages",
                model=model,
                api_key=api_key or None,
            )

            status.write("Step 3/3: Compose LaTeX document")
            compose(
                fragments=out_dir / "fragments",
                template=out_dir / "template.tex",
                out_tex=out_dir / "document.tex",
            )

            if compile_pdf:
                status.write("Compiling PDF with xelatex")
                try:
                    run(["xelatex", "document.tex"], cwd=out_dir)
                except FileNotFoundError:
                    status.write("xelatex not found on PATH; skipping PDF compile.")
                except subprocess.CalledProcessError as exc:
                    status.write(f"xelatex failed ({exc}); skipping PDF compile.")

            status.update(label="Conversion complete", state="complete")
        except Exception:
            status.update(label="Conversion failed", state="error")
            raise

        pdf_path = out_dir / "document.pdf"
        tex_path = out_dir / "document.tex"

        pdf_bytes = pdf_path.read_bytes() if pdf_path.exists() else None
        tex_bytes = tex_path.read_bytes() if tex_path.exists() else None
        pages: list[dict[str, str]] = []
        pages_dir = out_dir / "pages"
        if pages_dir.exists():
            for page_file in sorted(pages_dir.glob("*.txt")):
                pages.append({"name": page_file.name, "content": page_file.read_text()})

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in out_dir.rglob("*"):
                zf.write(path, arcname=path.relative_to(out_dir))
        zip_buf.seek(0)

        return {
            "pdf_bytes": pdf_bytes,
            "tex_bytes": tex_bytes,
            "zip_bytes": zip_buf.getvalue(),
            "pdf_name": f"{input_path.stem}.pdf",
            "tex_name": f"{input_path.stem}.tex",
            "zip_name": f"{input_path.stem}_ppt2tex.zip",
            "pages": pages,
        }


def main():
    st.set_page_config(page_title="ppt2tex UI", page_icon="📄")
    st.title("ppt2tex: PPT → LaTeX notes")
    st.write("Upload or drag-and-drop a .ppt/.pptx to convert using the ppt2tex pipeline.")

    uploaded = st.file_uploader("Upload PPT/PPTX", type=["ppt", "pptx"])
    dpi = st.slider("Slide render DPI", min_value=150, max_value=400, value=300, step=25)
    model_choice = st.selectbox(
        "Gemini model",
        options=[
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-3-flash-preview",
            "Custom (enter below)",
        ],
        index=0,
    )
    custom_model = st.text_input("Custom model (if selected above)", value=DEFAULT_MODEL)
    model = custom_model if model_choice == "Custom (enter below)" else model_choice
    api_key = st.text_input(
        "Gemini API key (optional if you only want placeholders)",
        type="password",
        help="Looks for GEMINI_API_KEY or GOOGLE_API_KEY if left blank.",
    )
    compile_pdf = st.checkbox("Compile PDF with xelatex", value=False)

    if st.button("Convert"):
        if not uploaded:
            st.warning("Please upload a .ppt/.pptx file first.")
        else:
            with st.spinner("Converting..."):
                try:
                    result = run_pipeline(
                        uploaded_file=uploaded,
                        dpi=int(dpi),
                        model=model,
                        api_key=api_key or None,
                        compile_pdf=compile_pdf,
                    )
                    st.success("Conversion finished.")
                    if result["pdf_bytes"]:
                        st.download_button(
                            "Download PDF",
                            data=result["pdf_bytes"],
                            file_name=result["pdf_name"],
                            mime="application/pdf",
                        )
                    elif result["tex_bytes"]:
                        st.info("PDF not available; download LaTeX instead.")
                        st.download_button(
                            "Download LaTeX",
                            data=result["tex_bytes"],
                            file_name=result["tex_name"],
                            mime="application/x-tex",
                        )
                    st.download_button(
                        "Download full output (zip)",
                        data=result["zip_bytes"],
                        file_name=result["zip_name"],
                        mime="application/zip",
                    )
                    if result["pages"]:
                        st.subheader("Per-page OCR text outputs")
                        for page in result["pages"]:
                            st.download_button(
                                f"Download {page['name']}",
                                data=page["content"],
                                file_name=page["name"],
                                mime="text/plain",
                            )
                            st.text_area(
                                label=page["name"],
                                value=page["content"],
                                height=120,
                                key=page["name"],
                            )
                except Exception as exc:  # pragma: no cover - defensive UI handling
                    st.error(f"Conversion failed: {exc}")


if __name__ == "__main__":
    main()
