"""Cover image extraction for uploaded ebooks."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(root: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return next(
        (node for node in root.iter() if _local_name(node.tag) == name), None
    )


def _extract_epub_cover(source_path: Path, output_dir: Path) -> bool:
    """Extract cover image from an EPUB file. Returns True on success."""
    try:
        with zipfile.ZipFile(source_path, "r") as zf:
            # Read container.xml to find OPF path
            container_path = None
            for name in zf.namelist():
                if name.endswith("container.xml"):
                    container_path = name
                    break
            if not container_path:
                return False

            container_xml = zf.read(container_path)
            container_root = ElementTree.fromstring(container_xml)
            rootfile = _find(container_root, "rootfile")
            if rootfile is None:
                return False

            opf_path = rootfile.get("full-path", "")
            if not opf_path or opf_path not in zf.namelist():
                return False

            # Parse OPF to find cover image
            opf_xml = zf.read(opf_path)
            opf_root = ElementTree.fromstring(opf_xml)

            # Find the OPF directory (for resolving relative paths)
            opf_dir = Path(opf_path).parent.as_posix()
            if opf_dir == ".":
                opf_dir = ""

            # Strategy 1: <meta name="cover"> → <item id="...">
            cover_id = None
            for meta in opf_root.iter():
                if _local_name(meta.tag) == "meta" and meta.get("name") == "cover":
                    cover_id = meta.get("content", "")
                    break

            # Strategy 2: <item properties="cover-image">
            cover_href = None
            manifest = _find(opf_root, "manifest")
            if manifest is not None:
                for item in manifest.iter():
                    if _local_name(item.tag) != "item":
                        continue
                    props = item.get("properties", "")
                    if cover_id and item.get("id") == cover_id:
                        cover_href = item.get("href", "")
                        break
                    if "cover-image" in props:
                        cover_href = item.get("href", "")
                        break

            if not cover_href:
                # Strategy 3: look for any image file named "cover"
                for name in zf.namelist():
                    stem = Path(name).stem.lower()
                    if stem == "cover" and Path(name).suffix.lower() in (
                        ".jpg", ".jpeg", ".png", ".gif", ".webp",
                    ):
                        cover_href = name
                        if opf_dir:
                            cover_href = str(Path(name).relative_to(opf_dir))
                        break

            if not cover_href:
                return False

            # Resolve the cover image path relative to OPF
            cover_full_path = cover_href
            if opf_dir and not cover_href.startswith("/"):
                cover_full_path = f"{opf_dir}/{cover_href}"
            cover_full_path = cover_full_path.lstrip("/")

            # Try the resolved path, then try as relative to OPF
            candidates = [cover_full_path]
            if opf_dir:
                candidates.append(f"{opf_dir}/{cover_href}")
            # Also try matching by filename
            cover_name = Path(cover_href).name
            for name in zf.namelist():
                if Path(name).name == cover_name and name not in candidates:
                    candidates.append(name)

            cover_data = None
            for candidate in candidates:
                if candidate in zf.namelist():
                    cover_data = zf.read(candidate)
                    break

            if not cover_data:
                return False

            # Write the cover image
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / "cover.jpg"

            # If it's already JPEG, write directly; otherwise convert via Pillow
            if cover_href.lower().endswith((".jpg", ".jpeg")):
                output_path.write_bytes(cover_data)
            else:
                try:
                    from PIL import Image  # type: ignore[import-untyped]

                    img = Image.open(io.BytesIO(cover_data))
                    img = img.convert("RGB")
                    img.thumbnail((400, 600), Image.LANCZOS)  # type: ignore[attr-defined]
                    img.save(output_path, "JPEG", quality=85)
                except ImportError:
                    output_path.write_bytes(cover_data)

            return output_path.is_file()

    except (zipfile.BadZipFile, OSError, ElementTree.ParseError):
        return False


def _extract_pdf_cover(source_path: Path, output_dir: Path) -> bool:
    """Extract first page of a PDF as cover using pymupdf. Returns True on success."""
    try:
        import fitz  # pymupdf
    except ImportError:
        return False

    try:
        doc = fitz.open(str(source_path))  # type: ignore[attr-defined]
        if len(doc) == 0:
            doc.close()
            return False

        page = doc[0]
        pix = page.get_pixmap(dpi=150)
        output_dir.mkdir(parents=True, exist_ok=True)
        pix.save(str(output_dir / "cover.jpg"))
        doc.close()
        return (output_dir / "cover.jpg").is_file()
    except Exception:
        return False


def extract_cover(source_path: Path, output_dir: Path) -> str | None:
    """
    Extract cover image from an ebook file.

    Returns the relative path ".onebookwiki/cover.jpg" on success, None on failure.
    """
    suffix = source_path.suffix.lower()
    success = False

    if suffix == ".epub":
        success = _extract_epub_cover(source_path, output_dir)
    elif suffix == ".pdf":
        success = _extract_pdf_cover(source_path, output_dir)

    if success:
        return ".onebookwiki/cover.jpg"
    return None
