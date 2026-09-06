from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


def paragraph(text: str, properties: str = "", runs: str = "") -> str:
    return f'<w:p>{properties}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>{runs}</w:p>'


def make_docx(
    path: Path,
    body: str,
    *,
    styles: str | None = None,
    numbering: str | None = None,
    footnotes: str | None = None,
    relationships: str = "",
    extra_parts: dict[str, bytes] | None = None,
    extra_types: str = "",
) -> Path:
    """Create a minimal real OPC package, without an Office dependency."""
    main_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    parts: dict[str, bytes] = {
        "word/document.xml": f'<w:document xmlns:w="{WORD_NS}" xmlns:r="{REL_NS}"><w:body>{body}</w:body></w:document>'.encode(),
        "_rels/.rels": f'<Relationships xmlns="{PACKAGE_NS}"><Relationship Id="main" Type="{REL_NS}/officeDocument" Target="word/document.xml"/></Relationships>'.encode(),
    }
    overrides = f'<Override PartName="/word/document.xml" ContentType="{main_type}"/>'
    rels = relationships
    for kind, content in (("styles", styles), ("numbering", numbering), ("footnotes", footnotes)):
        if content is None:
            continue
        name = f"word/{kind}.xml"
        parts[name] = (
            f'<w:{kind} xmlns:w="{WORD_NS}" xmlns:r="{REL_NS}">{content}</w:{kind}>'.encode()
        )
        overrides += f'<Override PartName="/{name}" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}+xml"/>'
        rels += f'<Relationship Id="{kind}" Type="{REL_NS}/{kind}" Target="{kind}.xml"/>'
    if rels:
        parts["word/_rels/document.xml.rels"] = (
            f'<Relationships xmlns="{PACKAGE_NS}">{rels}</Relationships>'.encode()
        )
    parts["[Content_Types].xml"] = (
        f'<Types xmlns="{CONTENT_NS}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>{overrides}{extra_types}</Types>'.encode()
    )
    parts.update(extra_parts or {})
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, raw in parts.items():
            archive.writestr(name, raw)
    return path
