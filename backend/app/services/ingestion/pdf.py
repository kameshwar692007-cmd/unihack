from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

class PDFElement:
    def __init__(self, text: str, page_num: int, element_type: str, metadata: dict | None = None):
        self.text = text
        self.page_num = page_num
        self.element_type = element_type  # "paragraph", "table", "heading", etc.
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "page_num": self.page_num,
            "element_type": self.element_type,
            "metadata": self.metadata,
        }


def chunk_pdf_elements(
    elements: List[PDFElement],
    max_chars: int = 1200,
    overlap: int = 150,
) -> List[PDFElement]:
    """Split long document elements while retaining their page provenance."""
    if max_chars <= 0 or overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be non-negative and smaller than max_chars")

    chunks: List[PDFElement] = []
    for element in elements:
        text = " ".join(element.text.split())
        if not text:
            continue
        if len(text) <= max_chars:
            chunks.append(element)
            continue

        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            if end < len(text):
                boundary = text.rfind(" ", start, end)
                if boundary > start:
                    end = boundary
            chunks.append(PDFElement(
                text=text[start:end].strip(),
                page_num=element.page_num,
                element_type=element.element_type,
                metadata={**element.metadata, "chunk_start": start, "chunk_end": end},
            ))
            if end >= len(text):
                break
            start = max(end - overlap, start + 1)
    return chunks


class PDFProcessor:
    """Processes manufacturer PDFs and extracts text, tables, page numbers, and metadata."""

    _cache: dict[str, List[PDFElement]] = {}

    @classmethod
    def process_pdf(cls, file_path: str | Path) -> List[PDFElement]:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"PDF file not found: {path}")

        import hashlib
        file_hash = None
        try:
            file_hash = hashlib.md5(path.read_bytes()).hexdigest()
            if file_hash in cls._cache:
                logger.info(f"Using cached PDF elements for {path.name}")
                return cls._cache[file_hash]
        except Exception as cache_err:
            logger.warning(f"Failed to calculate PDF file hash for cache: {cache_err}")

        elements = cls._process_pdf_raw(path)
        if file_hash and elements:
            cls._cache[file_hash] = elements
        return elements

    @classmethod
    def _process_pdf_raw(cls, path: Path) -> List[PDFElement]:
        elements: List[PDFElement] = []
        
        # Fast-track using PyPDF first for high performance
        try:
            import pypdf
            reader = pypdf.PdfReader(path)
            for page_idx, page in enumerate(reader.pages):
                page_num = page_idx + 1
                text = page.extract_text()
                if text and text.strip():
                    paragraphs = text.split("\n\n")
                    for p in paragraphs:
                        clean_p = p.strip()
                        if clean_p:
                            elements.append(
                                PDFElement(
                                    text=clean_p,
                                    page_num=page_num,
                                    element_type="paragraph",
                                    metadata={"source": path.name, "fallback": False}
                                )
                            )
            if elements:
                logger.info(f"Loaded {len(elements)} elements using fast PyPDF from {path.name}")
                return chunk_pdf_elements(elements)
        except Exception as pypdf_err:
            logger.warning(f"PyPDF issue for {path.name}: {pypdf_err}")

        # Fallback to Docling if installed
        try:
            from docling.document_converter import DocumentConverter
            logger.info(f"Processing {path.name} using Docling fallback...")
            converter = DocumentConverter()
            result = converter.convert(str(path))
            doc = result.document
            for element, level in doc.iterate_items():
                page_num = 1
                if hasattr(element, "prov") and element.prov:
                    page_num = element.prov[0].page_no if hasattr(element.prov[0], "page_no") else 1
                text = element.text if hasattr(element, "text") else str(element)
                if text.strip():
                    elements.append(PDFElement(text=text.strip(), page_num=page_num, element_type="paragraph"))
            if elements:
                return chunk_pdf_elements(elements)
        except Exception as docling_err:
            logger.warning(f"Docling fallback skipped: {docling_err}")

        # Emergency raw text extraction fallback for synthetic/minimal PDFs
        try:
            raw_bytes = path.read_bytes()
            import re
            matches = re.findall(rb"\(([^\(\)]+)\)\s*Tj", raw_bytes)
            if matches:
                extracted_str = " ".join([m.decode("utf-8", "ignore") for m in matches if m.strip()])
                if extracted_str.strip():
                    elements.append(
                        PDFElement(
                            text=extracted_str.strip(),
                            page_num=1,
                            element_type="paragraph",
                            metadata={"source": path.name, "fallback": True}
                        )
                    )
        except Exception as raw_err:
            logger.warning(f"Raw string extraction fallback failed for {path.name}: {raw_err}")

        return chunk_pdf_elements(elements)
