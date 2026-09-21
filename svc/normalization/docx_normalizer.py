# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""DOCX normalization (plan section 7): addressable blocks and the model-facing tagged text.

docx2python (html=True, so italic and similar run formatting survives) supplies the text; an lxml
pass over the same package supplies what it does not report: which paragraph a note is attached to,
table coordinates, text-box hosts, and the quality gates that ask for the LibreOffice fallback.

Blocks, in document order, each individually addressable by `source_id`:

  p-0001            body paragraph                 tc-0001-0002-0001   table cell (table-row-column)
  fn-12 / en-3      footnote / endnote by w:id     tx-0001             text-box paragraph (anchor: its host)
  hdr-1 / ftr-1     header / footer (kept for completeness, not sent to the model by default)

A note follows the paragraph (or, for a table cell, the table) holding its first reference, never a
block of its own at the end of the document. Identity comes from w:id, not from counting, so numbering
restarts and custom marks cannot mislabel a note.
"""

from __future__ import annotations

import html
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Sequence, Set, Tuple

from svc.normalization.config import NormalizationConfig
from svc.normalization.models import DocumentBlock, SourceLocation, UnsupportedDocumentError
from svc.normalization.text import escape_delimiters, normalize_for_match
from utils.logger import get_logger

logger = get_logger()

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_MAX_XML_PART_BYTES = 64 * 1024 * 1024


def _q(tag: str) -> str:
    return f"{{{_W}}}{tag}"


_MARKER_RE = re.compile(r"----(footnote|endnote)(-?\d+)----")
_LABEL_RE = re.compile(r"^((?:<[^>]*>)*)(?:footnote|endnote)(-?\d+)\)\t")
_TAG_SPLIT_RE = re.compile(r"(<[^>]*>)")
_TAG_NAME_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9]*)")
_HREF_RE = re.compile(r'href="([^"]*)"')
_KEEP_TAGS = {"i": "italic", "b": "bold", "u": "underline", "sup": "superscript", "sub": "subscript"}
_NOTE_PREFIX = {"footnote": "fn", "endnote": "en"}

# DrawingML graphicData parts that carry text the paragraph parser never sees (text boxes are
# detected separately, through w:txbxContent).
_DRAWING_TEXT_URIS = ("/diagram", "/chart")

# The order in which fallback reasons are reported: the first present one is the fallback_reason.
_REASON_PRIORITY = (
    "docx_parser_error", "unresolved_footnote_references", "text_box_content", "embedded_object",
    "drawing_text", "implausibly_low_text", "page_numbers_required",
)


@dataclass
class Rendered:
    raw: str
    model: str
    refs: List[Tuple[str, str]]
    links: List[str]
    formats: Set[str]


def render_paragraph(s: str) -> Rendered:
    """One docx2python (html=True) paragraph string -> plain text, model text, note references, formats.

    The plain text drops tags and note markers and undoes docx2python's HTML escaping. The model text keeps
    italic/bold/underline/sup/sub/small-caps as tags, replaces each note marker with a reference tag and
    escapes "<" and ">" so document text can't be mistaken for a tag.
    """
    raw: List[str] = []
    model: List[str] = []
    refs: List[Tuple[str, str]] = []
    links: List[str] = []
    formats: Set[str] = set()
    span_stack: List[bool] = []

    def text(chunk: str) -> None:
        plain = html.unescape(chunk)
        raw.append(plain)
        model.append(escape_delimiters(plain))

    for token in _TAG_SPLIT_RE.split(s):
        if not token:
            continue
        name_match = _TAG_NAME_RE.match(token) if token.startswith("<") and token.endswith(">") else None
        if name_match is None:
            position = 0
            for marker in _MARKER_RE.finditer(token):
                text(token[position:marker.start()])
                kind, ident = marker.group(1), marker.group(2)
                refs.append((kind, ident))
                model.append(f'<{kind}-ref id="{_NOTE_PREFIX[kind]}-{ident}" />')
                position = marker.end()
            text(token[position:])
            continue
        closing, name = bool(name_match.group(1)), name_match.group(2).lower()
        if name in _KEEP_TAGS:
            model.append(f"</{name}>" if closing else f"<{name}>")
            if not closing:
                formats.add(_KEEP_TAGS[name])
        elif name == "span":
            if closing:
                was_small_caps = span_stack.pop() if span_stack else False
                if was_small_caps:
                    model.append("</sc>")
            else:
                small_caps = "small-caps" in token.lower()
                span_stack.append(small_caps)
                if small_caps:
                    model.append("<sc>")
                    formats.add("small_caps")
        elif name == "a" and not closing:
            href = _HREF_RE.search(token)
            if href:
                links.append(html.unescape(href.group(1)))
    return Rendered("".join(raw), "".join(model), refs, links, formats)


def _iter_cells(strings: Any, pars: Any) -> Iterator[Tuple[List[str], List[Any]]]:
    """Leaf lists of docx2python's nested output: (paragraph strings, matching Par objects)."""
    if not isinstance(strings, list):
        return
    if strings and all(isinstance(item, str) for item in strings):
        yield strings, pars
        return
    for s, p in zip(strings, pars):
        yield from _iter_cells(s, p)


def _count_leaves(nested: Any) -> int:
    if isinstance(nested, list):
        return sum(_count_leaves(item) for item in nested)
    return 1


def _ancestor(elem: Any, tag: str) -> Any | None:
    for a in elem.iterancestors():
        if a.tag == tag:
            return a
    return None


def _outer_host(elem: Any) -> Any | None:
    """The body paragraph a text-box paragraph hangs off (through nested text boxes)."""
    box = _ancestor(elem, _q("txbxContent"))
    host = _ancestor(box, _q("p")) if box is not None else None
    while host is not None and _ancestor(host, _q("txbxContent")) is not None:
        host = _ancestor(_ancestor(host, _q("txbxContent")), _q("p"))
    return host


@dataclass
class _Entry:
    rendered: Rendered
    elem: Any
    cell: Any | None = None  # the w:tc holding it (a table cell paragraph), else None
    text_box_host: Any | None = None  # the host w:p when this is a text-box paragraph
    attached_boxes: List["_Entry"] = field(default_factory=list)


@dataclass
class DocxResult:
    blocks: List[DocumentBlock] = field(default_factory=list)
    tagged_text: str = ""
    fallback_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    telemetry: Dict[str, Any] = field(default_factory=dict)


class DocxNormalizer:
    def __init__(self, config: NormalizationConfig) -> None:
        self.config = config

    # --- package safety (plan section 14) -----------------------------------------------

    def check_package(self, path: str) -> None:
        """Reject a malformed, encrypted or hostile archive before anything is decompressed."""
        cfg = self.config
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise UnsupportedDocumentError("docx_invalid", "This file is not a valid Word document (.docx).") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > cfg.max_docx_entries:
                raise UnsupportedDocumentError("docx_too_many_entries", "This Word document is malformed (too many parts).")
            total = 0
            for info in infos:
                if info.flag_bits & 0x1:
                    raise UnsupportedDocumentError("docx_encrypted", "Password-protected Word documents are not supported.")
                total += info.file_size
                if info.file_size > 1024 * 1024 and info.file_size / max(info.compress_size, 1) > cfg.max_compression_ratio:
                    raise UnsupportedDocumentError("docx_zip_bomb", "This Word document is malformed (excessive compression).")
                if info.filename.lower().endswith((".xml", ".rels")) and info.file_size > _MAX_XML_PART_BYTES:
                    raise UnsupportedDocumentError("docx_part_too_large", "This Word document is too large to process.")
            if total > cfg.max_docx_uncompressed_bytes:
                raise UnsupportedDocumentError("docx_too_large_uncompressed", "This Word document is too large to process.")
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise UnsupportedDocumentError("docx_invalid", "This file is not a valid Word document (.docx).")
            for info in infos:
                if info.filename.lower().endswith(".xml") and info.file_size:
                    with archive.open(info) as part:
                        head = part.read(4096)
                    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
                        raise UnsupportedDocumentError("docx_dtd", "This Word document is malformed (unexpected declarations).")

    # --- quality-gate scan ---------------------------------------------------------------

    @staticmethod
    def _scan_gates(path: str) -> Tuple[List[str], int]:
        """(fallback reasons visible in the package XML, size of document.xml)."""
        from lxml import etree

        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, remove_comments=True)
        reasons: List[str] = []
        xml_bytes = 0
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for name in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"):
                if name not in names:
                    continue
                data = archive.read(name)
                if name == "word/document.xml":
                    xml_bytes = len(data)
                try:
                    root = etree.fromstring(data, parser)
                except etree.XMLSyntaxError:
                    reasons.append("docx_parser_error")
                    continue
                if next(root.iter(_q("txbxContent")), None) is not None:
                    reasons.append("text_box_content")
                if next(root.iter(_q("object")), None) is not None or root.xpath("//*[local-name()='oleObj']"):
                    reasons.append("embedded_object")
                for graphic_data in root.xpath("//*[local-name()='graphicData']"):
                    if any((graphic_data.get("uri") or "").endswith(suffix) for suffix in _DRAWING_TEXT_URIS):
                        reasons.append("drawing_text")
                        break
        return list(dict.fromkeys(reasons)), xml_bytes

    # --- main entry ----------------------------------------------------------------------

    def normalize(self, path: str) -> DocxResult:
        """Blocks and tagged text for a .docx, or (parser failure) an empty result asking for a fallback.

        Raises UnsupportedDocumentError for an archive that must be rejected outright.
        """
        self.check_package(path)
        result = DocxResult()
        gate_reasons, xml_bytes = self._scan_gates(path)
        result.telemetry["docx_xml_bytes"] = xml_bytes
        try:
            self._extract(path, result)
        except UnsupportedDocumentError:
            raise
        except Exception as exc:  # the parser choked: the caller decides whether a fallback exists
            logger.warning("DOCX parser failed: %s", type(exc).__name__)
            result.blocks, result.tagged_text = [], ""
            result.fallback_reasons = ["docx_parser_error"]
            return result

        reasons = list(gate_reasons)
        if result.telemetry.pop("unresolved_note_refs", 0):
            reasons.append("unresolved_footnote_references")
        chars = sum(len(b.raw_text) for b in result.blocks)
        result.telemetry["docx_extracted_chars"] = chars
        if xml_bytes >= 20_000 and chars < 0.01 * xml_bytes:
            reasons.append("implausibly_low_text")
        if self.config.require_page_numbers:
            reasons.append("page_numbers_required")
        result.fallback_reasons = sorted(
            dict.fromkeys(reasons), key=lambda r: _REASON_PRIORITY.index(r) if r in _REASON_PRIORITY else len(_REASON_PRIORITY)
        )
        return result

    # --- extraction ----------------------------------------------------------------------

    def _extract(self, path: str, result: DocxResult) -> None:
        from docx2python import docx2python

        with docx2python(path, html=True) as content:
            if _count_leaves(content.body) != _count_leaves(content.body_pars):
                raise ValueError("docx2python body and body_pars disagree")
            entries = self._body_entries(content.body, content.body_pars)
            notes = {
                "footnote": self._note_bodies(content.footnotes, content.footnotes_pars),
                "endnote": self._note_bodies(content.endnotes, content.endnotes_pars),
            }
            headers = self._plain_parts(content.header)
            footers = self._plain_parts(content.footer)
        result.blocks, result.tagged_text = self._assemble(entries, notes, headers, footers, result)

    @staticmethod
    def _body_entries(strings: Any, pars: Any) -> List[_Entry]:
        entries: List[_Entry] = []
        for cell_strings, cell_pars in _iter_cells(strings, pars):
            for s, par in zip(cell_strings, cell_pars):
                elem = par.elem
                if _ancestor(elem, f"{{{_MC}}}Fallback") is not None:
                    continue  # the VML copy of a text box the DrawingML branch already carries
                in_box = _ancestor(elem, _q("txbxContent")) is not None
                entries.append(_Entry(
                    render_paragraph(s), elem,
                    None if in_box else _ancestor(elem, _q("tc")),
                    _outer_host(elem) if in_box else None,
                ))
        return entries

    @staticmethod
    def _note_bodies(strings: Any, pars: Any) -> Dict[str, Rendered]:
        """{w:id: rendered body} for every real note (separators carry no label and are skipped)."""
        bodies: Dict[str, Rendered] = {}
        for cell_strings, _ in _iter_cells(strings, pars):
            label = _LABEL_RE.match(cell_strings[0])
            if label is None:
                continue
            pieces = [_LABEL_RE.sub(r"\1", cell_strings[0], count=1)] + list(cell_strings[1:])
            parts = [render_paragraph(piece) for piece in pieces]
            parts = [p for p in parts if p.raw.strip()]
            if not parts:
                continue
            parts[0].raw, parts[0].model = parts[0].raw.lstrip(), parts[0].model.lstrip()
            bodies[label.group(2)] = Rendered(
                "\n".join(p.raw for p in parts), "\n".join(p.model for p in parts),
                [r for p in parts for r in p.refs], [link for p in parts for link in p.links],
                set().union(*(p.formats for p in parts)),
            )
        return bodies

    @staticmethod
    def _plain_parts(strings: Any) -> List[str]:
        """Header/footer texts, one per part, as plain text."""
        out: List[str] = []
        for cell_strings, _ in _iter_cells(strings, strings):
            text = "\n".join(p.raw for p in (render_paragraph(s) for s in cell_strings) if p.raw.strip())
            if text.strip():
                out.append(text)
        return out

    # --- ordering, ids and tags ----------------------------------------------------------

    def _assemble(
        self,
        entries: Sequence[_Entry],
        notes: Dict[str, Dict[str, Rendered]],
        headers: Sequence[str],
        footers: Sequence[str],
        result: DocxResult,
    ) -> Tuple[List[DocumentBlock], str]:
        # A text box is listed before its host paragraph (and twice, Choice + Fallback). Attach each
        # surviving one to its host so it comes out once, right after it.
        hosts = {id(e.elem): e for e in entries if e.text_box_host is None}
        ordered: List[_Entry] = []
        for entry in entries:
            host = hosts.get(id(entry.text_box_host)) if entry.text_box_host is not None else None
            if host is not None:
                host.attached_boxes.append(entry)
            else:
                ordered.append(entry)

        root = entries[0].elem.getroottree().getroot() if entries else None
        table_number = {id(t): n for n, t in enumerate(root.iter(_q("tbl")), start=1)} if root is not None else {}

        blocks: List[DocumentBlock] = []
        tagged: List[str] = []
        anchored: Set[Tuple[str, str]] = set()
        unresolved: Set[Tuple[str, str]] = set()
        resolved = {"footnote": 0, "endnote": 0}
        counters = {"p": 0, "tx": 0}
        # Notes and text boxes attached to table cells are written once the table has closed.
        deferred: List[Tuple[List[Tuple[str, str]], List[_Entry], str]] = []
        table_state: Dict[str, int | None] = {"table": None, "row": None}

        def add_notes(refs: Sequence[Tuple[str, str]], anchor_id: str) -> None:
            for kind, ident in refs:
                if (kind, ident) in anchored:
                    continue
                anchored.add((kind, ident))
                body = notes[kind].get(ident)
                if body is None:
                    unresolved.add((kind, ident))
                    continue
                source_id = f"{_NOTE_PREFIX[kind]}-{ident}"
                blocks.append(self._block(kind, body, SourceLocation(source_id, note_id=ident), anchor_id))
                tagged.append(f'<{kind} id="{source_id}" anchor="{anchor_id}">{body.model}</{kind}>')
                resolved[kind] += 1

        def add_boxes(host: _Entry, anchor_id: str) -> None:
            for box in host.attached_boxes:
                if not box.rendered.raw.strip():
                    continue
                counters["tx"] += 1
                source_id = f"tx-{counters['tx']:04d}"
                blocks.append(self._block("paragraph", box.rendered, SourceLocation(source_id), anchor_id, text_box=True))
                tagged.append(f'<textbox id="{source_id}" anchor="{anchor_id}">{box.rendered.model}</textbox>')
                add_notes(box.rendered.refs, source_id)

        def close_table() -> None:
            if table_state["row"] is not None:
                tagged.append("</row>")
            if table_state["table"] is not None:
                tagged.append("</table>")
                for refs, boxes, anchor_id in deferred:
                    add_notes(refs, anchor_id)
                    for host in boxes:
                        add_boxes(host, anchor_id)
                deferred.clear()
            table_state["table"] = table_state["row"] = None

        index = 0
        while index < len(ordered):
            entry = ordered[index]
            if entry.cell is None:
                close_table()
                index += 1
                if not entry.rendered.raw.strip():
                    continue
                counters["p"] += 1
                source_id = f"p-{counters['p']:04d}"
                blocks.append(self._block("paragraph", entry.rendered, SourceLocation(source_id, paragraph_index=counters["p"])))
                tagged.append(f'<paragraph id="{source_id}">{entry.rendered.model}</paragraph>')
                add_notes(entry.rendered.refs, source_id)
                add_boxes(entry, source_id)
                continue

            # A table cell: every paragraph of the same w:tc becomes one block.
            cell = entry.cell
            members = [entry]
            while index + len(members) < len(ordered) and ordered[index + len(members)].cell is cell:
                members.append(ordered[index + len(members)])
            index += len(members)
            row = cell.getparent()
            table = row.getparent()
            t = table_number.get(id(table), 1)
            r = 1 + [x for x in table if x.tag == _q("tr")].index(row)
            c = 1 + [x for x in row if x.tag == _q("tc")].index(cell)
            if table_state["table"] != t:
                close_table()
                tagged.append(f'<table id="t-{t:04d}">')
                table_state["table"] = t
            if table_state["row"] != r:
                if table_state["row"] is not None:
                    tagged.append("</row>")
                tagged.append("<row>")
                table_state["row"] = r
            parts = [m.rendered for m in members if m.rendered.raw.strip()]
            source_id = f"tc-{t:04d}-{r:04d}-{c:04d}"
            boxes = [m for m in members if m.attached_boxes]
            if parts:
                merged = Rendered(
                    "\n".join(p.raw for p in parts), "\n".join(p.model for p in parts),
                    [x for p in parts for x in p.refs], [x for p in parts for x in p.links],
                    set().union(*(p.formats for p in parts)),
                )
                blocks.append(self._block(
                    "table_cell", merged, SourceLocation(source_id, table_index=t, row_index=r, column_index=c)))
                tagged.append(f'<cell id="{source_id}">{merged.model}</cell>')
                deferred.append(([x for p in parts for x in p.refs], boxes, source_id))
            elif boxes:
                deferred.append(([], boxes, source_id))
        close_table()

        for n, text in enumerate(headers, start=1):
            blocks.append(DocumentBlock("header", text, normalize_for_match(text).text, SourceLocation(f"hdr-{n}")))
        for n, text in enumerate(footers, start=1):
            blocks.append(DocumentBlock("footer", text, normalize_for_match(text).text, SourceLocation(f"ftr-{n}")))

        result.telemetry.update({
            "docx_block_count": sum(1 for b in blocks if b.kind not in ("header", "footer")),
            "footnote_count": resolved["footnote"],
            "endnote_count": resolved["endnote"],
            "orphan_note_count": sum(len(bodies) for bodies in notes.values()) - sum(resolved.values()),
            "table_count": len(table_number),
            "text_box_count": counters["tx"],
            "unresolved_note_refs": len(unresolved),
        })
        if self.config.include_headers_footers:
            tagged = (
                [f'<header id="hdr-{n}">{escape_delimiters(t)}</header>' for n, t in enumerate(headers, start=1)]
                + tagged
                + [f'<footer id="ftr-{n}">{escape_delimiters(t)}</footer>' for n, t in enumerate(footers, start=1)]
            )
        return blocks, '<document source="document.docx">\n' + "\n".join(tagged) + "\n</document>"

    @staticmethod
    def _block(kind: str, rendered: Rendered, location: SourceLocation, anchor: str | None = None,
               text_box: bool = False) -> DocumentBlock:
        formatting: Dict[str, bool | str] = {name: True for name in sorted(rendered.formats)}
        if rendered.links:
            formatting["hyperlinks"] = " ".join(rendered.links)
        if text_box:
            formatting["text_box"] = True
        return DocumentBlock(kind, rendered.raw, normalize_for_match(rendered.raw).text, location, anchor, formatting)  # type: ignore[arg-type]
