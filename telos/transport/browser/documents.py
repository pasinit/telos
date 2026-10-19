"""Text-only PDF/ODT extraction for browser downloads; never execute attachments."""

import asyncio
import sys
import zipfile
from pathlib import Path
from xml.parsers import expat

import pymupdf

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_PAGES = 200
MAX_DOCUMENT_CHARS = 500_000
MAX_ARCHIVE_ENTRIES = 1024
MAX_UNCOMPRESSED_BYTES = 40 * 1024 * 1024
ODT_MIMETYPE = b"application/vnd.oasis.opendocument.text"
OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"


async def extract_document(path: Path) -> str:
    """Isolate native parsing: PyMuPDF is not thread-safe, and parsing is bounded."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "telos.transport.browser.documents", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise ValueError(stderr.decode(errors="replace").strip() or "Document extraction failed")
    return stdout.decode("utf-8")


async def extract_pdf(path: Path) -> str:
    """Compatibility entry point for existing PDF callers."""
    return await extract_document(path)


def read_document(path: Path) -> str:
    """Detect by contents, not the untrusted filename of a browser attachment."""
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds the 20 MB size limit")
    with path.open("rb") as handle:
        signature = handle.read(5)
    if signature == b"%PDF-":
        return read_pdf(path)
    if signature.startswith(b"PK"):
        return read_odt(path)
    raise ValueError("Unsupported attachment format; only PDF and ODT text extraction is available")


def read_odt(path: Path) -> str:
    """Read only ODT body text; no archive extraction, scripts, or XML entities.

    The subprocess deadline also bounds ZIP and XML parsing. Metadata and actual
    reads are bounded separately; unrelated embedded archive content is not read.
    """
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds the 20 MB size limit")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise ValueError("ODT exceeds the archive entry limit")
            if len({entry.filename for entry in entries}) != len(entries):
                raise ValueError("ODT contains duplicate archive entries")
            if sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("ODT exceeds the decompressed size limit")
            with archive.open("mimetype") as handle:
                if handle.read(len(ODT_MIMETYPE) + 1) != ODT_MIMETYPE:
                    raise ValueError("Unsupported attachment format; archive is not ODT")
            with archive.open("content.xml") as handle:
                content = handle.read(MAX_UNCOMPRESSED_BYTES + 1)
            if len(content) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("ODT exceeds the decompressed size limit")
    except (zipfile.BadZipFile, KeyError, RuntimeError, NotImplementedError) as exc:
        raise ValueError(f"Malformed or unsupported ODT archive: {exc}") from exc

    chunks = []
    length = 0
    stack = []
    body_depth = 0
    paragraph_depth = 0

    def emit(text):
        nonlocal length
        length += len(text)
        if length > MAX_DOCUMENT_CHARS:
            raise ValueError("ODT text exceeds the 500000 character limit")
        chunks.append(text)

    def start(name, attrs):
        nonlocal body_depth, paragraph_depth
        stack.append(name)
        if name == f"{OFFICE_NS}|text":
            body_depth += 1
        if not body_depth:
            return
        if name in (f"{TEXT_NS}|p", f"{TEXT_NS}|h"):
            paragraph_depth += 1
        elif paragraph_depth and name == f"{TEXT_NS}|s":
            count = int(attrs.get(f"{TEXT_NS}|c", "1"))
            if count < 1 or count > MAX_DOCUMENT_CHARS - length:
                raise ValueError("ODT space count exceeds the text limit")
            emit(" " * count)
        elif paragraph_depth and name == f"{TEXT_NS}|tab":
            emit("\t")
        elif paragraph_depth and name == f"{TEXT_NS}|line-break":
            emit("\n")

    def end(name):
        nonlocal body_depth, paragraph_depth
        if body_depth:
            if name in (f"{TEXT_NS}|p", f"{TEXT_NS}|h"):
                emit("\n")
                paragraph_depth -= 1
            elif name == f"{TABLE_NS}|table-cell":
                emit("\t")
            elif name == f"{TABLE_NS}|table-row":
                emit("\n")
        if name == f"{OFFICE_NS}|text":
            body_depth -= 1
        stack.pop()

    def characters(text):
        # Only text vocabulary and inline spans are accepted, never embedded
        # objects, scripts, annotations, or other non-document payloads.
        if body_depth and paragraph_depth and all(
            tag.startswith(TEXT_NS + "|") for tag in stack[stack.index(f"{OFFICE_NS}|text") + 1:]
            if not tag.startswith(TABLE_NS + "|")
        ):
            emit(text)

    def reject_entities(*args):
        raise ValueError("ODT XML declarations and entities are not supported")

    parser = expat.ParserCreate(namespace_separator="|")
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.StartDoctypeDeclHandler = reject_entities
    parser.EntityDeclHandler = reject_entities
    parser.ExternalEntityRefHandler = reject_entities
    try:
        parser.Parse(content, True)
    except expat.ExpatError as exc:
        raise ValueError(f"Malformed ODT XML: {exc}") from exc
    text = "".join(chunks).strip()
    if not text:
        raise ValueError("ODT contains no extractable text")
    return text


def read_pdf(path: Path) -> str:
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("Document exceeds the 20 MB size limit")
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise ValueError("Unsupported attachment format; only PDF text extraction is available")
    with pymupdf.open(path) as document:
        if document.needs_pass:
            raise ValueError("PDF is password protected")
        if len(document) > MAX_DOCUMENT_PAGES:
            raise ValueError("PDF exceeds the 200 page limit")
        pages = []
        length = 0
        has_text = False
        for number, page in enumerate(document, start=1):
            text = page.get_text()
            if text.strip():
                has_text = True
            else:
                text = "[No extractable text: this page may be blank or require OCR; requirements are not verified.]"
            pages.append(f"[Page {number}]\n{text}")
            length += len(pages[-1]) + (2 if number > 1 else 0)
            if length > MAX_DOCUMENT_CHARS:
                raise ValueError("PDF text exceeds the 500000 character limit")
    if not has_text:
        raise ValueError("PDF contains no extractable text; scanned pages require OCR")
    return "\n\n".join(pages)


if __name__ == "__main__":
    try:
        sys.stdout.buffer.write(read_document(Path(sys.argv[1])).encode("utf-8"))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
