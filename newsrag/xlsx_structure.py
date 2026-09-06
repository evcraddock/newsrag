"""Closed structural profile for the SpreadsheetML trees we accept.

OPC validates every part/namespace. This layer also validates nested content models
and inert attribute types, so an allowed metadata container cannot hide malformed
or unsupported workbook structures. It does not implement display formatting.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections import Counter
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from lxml import etree  # type: ignore[import-untyped]

from newsrag.adapters import AdapterError
from newsrag.xlsx_package import REL_NS, XLSX_NS
from newsrag.xlsx_structure_rules import (
    CHILD_COUNTS,
    CHILD_ORDER,
    ENUMERATIONS,
    HEX_LENGTHS,
    INTEGER_RANGES,
    PATTERNS,
    REQUIRED_ATTRIBUTES,
    SIMPLE_TYPES,
)

S = "{" + XLSX_NS + "}"
R = "{" + REL_NS + "}id"
SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _rule(children: str = "", attributes: str = "") -> tuple[frozenset[str], frozenset[str]]:
    return frozenset(children.split()), frozenset(attributes.split())


_RULES = {
    "worksheet": _rule(
        "sheetPr dimension sheetViews sheetFormatPr cols sheetData sheetProtection autoFilter sortState mergeCells phoneticPr printOptions pageMargins pageSetup headerFooter rowBreaks colBreaks ignoredErrors hyperlinks tableParts"
    ),
    "sheetPr": _rule(
        "tabColor outlinePr pageSetUpPr",
        "syncHorizontal syncVertical syncRef transitionEvaluation transitionEntry published codeName filterMode enableFormatConditionsCalculation",
    ),
    "tabColor": _rule(attributes="auto indexed rgb theme tint"),
    "outlinePr": _rule(attributes="applyStyles summaryBelow summaryRight showOutlineSymbols"),
    "pageSetUpPr": _rule(attributes="autoPageBreaks fitToPage"),
    "dimension": _rule(attributes="ref"),
    "sheetViews": _rule("sheetView"),
    "sheetView": _rule(
        "pane selection",
        "windowProtection showFormulas showGridLines showRowColHeaders showZeros rightToLeft tabSelected showRuler showOutlineSymbols defaultGridColor showWhiteSpace view topLeftCell colorId zoomScale zoomScaleNormal zoomScaleSheetLayoutView zoomScalePageLayoutView workbookViewId",
    ),
    "pane": _rule(attributes="xSplit ySplit topLeftCell activePane state"),
    "selection": _rule(attributes="pane activeCell activeCellId sqref"),
    "sheetFormatPr": _rule(
        attributes="baseColWidth defaultColWidth defaultRowHeight customHeight zeroHeight thickTop thickBottom outlineLevelRow outlineLevelCol"
    ),
    "cols": _rule("col"),
    "col": _rule(
        attributes="min max width style hidden bestFit customWidth phonetic outlineLevel collapsed"
    ),
    "sheetData": _rule("row"),
    "row": _rule(
        "c",
        "r spans s customFormat ht hidden customHeight outlineLevel collapsed thickTop thickBot ph",
    ),
    "c": _rule("f v is", "r s t cm vm ph"),
    "f": _rule(attributes="t ref si aca ca bx"),
    "v": _rule(),
    "is": _rule("t r rPh phoneticPr"),
    "si": _rule("t r rPh phoneticPr"),
    "r": _rule("rPr t"),
    "t": (frozenset(), frozenset({SPACE})),
    "rPh": _rule("t", "sb eb"),
    "phoneticPr": _rule(attributes="fontId type alignment"),
    "rPr": _rule(
        "rFont charset family b i strike outline shadow condense extend color sz u vertAlign scheme"
    ),
    "rFont": _rule(attributes="val"),
    "charset": _rule(attributes="val"),
    "family": _rule(attributes="val"),
    "b": _rule(attributes="val"),
    "i": _rule(attributes="val"),
    "strike": _rule(attributes="val"),
    "outline": _rule(attributes="val"),
    "shadow": _rule(attributes="val"),
    "condense": _rule(attributes="val"),
    "extend": _rule(attributes="val"),
    "color": _rule(attributes="auto indexed rgb theme tint"),
    "sz": _rule(attributes="val"),
    "u": _rule(attributes="val"),
    "vertAlign": _rule(attributes="val"),
    "scheme": _rule(attributes="val"),
    "sheetProtection": _rule(
        attributes="algorithmName hashValue saltValue spinCount password sheet objects scenarios formatCells formatColumns formatRows insertColumns insertRows insertHyperlinks deleteColumns deleteRows selectLockedCells sort autoFilter pivotTables selectUnlockedCells"
    ),
    "autoFilter": _rule("filterColumn sortState", "ref"),
    "filterColumn": _rule(
        "filters top10 customFilters dynamicFilter colorFilter iconFilter",
        "colId hiddenButton showButton",
    ),
    "filters": _rule("filter dateGroupItem", "blank calendarType"),
    "filter": _rule(attributes="val"),
    "dateGroupItem": _rule(attributes="year month day hour minute second dateTimeGrouping"),
    "top10": _rule(attributes="top percent val filterVal"),
    "customFilters": _rule("customFilter", "and"),
    "customFilter": _rule(attributes="operator val"),
    "dynamicFilter": _rule(attributes="type val maxVal"),
    "colorFilter": _rule(attributes="dxfId cellColor"),
    "iconFilter": _rule(attributes="iconSet iconId"),
    "sortState": _rule("sortCondition", "columnSort caseSensitive sortMethod ref"),
    "sortCondition": _rule(attributes="descending sortBy ref customList dxfId iconSet iconId"),
    "mergeCells": _rule("mergeCell", "count"),
    "mergeCell": _rule(attributes="ref"),
    "printOptions": _rule(
        attributes="horizontalCentered verticalCentered headings gridLines gridLinesSet"
    ),
    "pageMargins": _rule(attributes="left right top bottom header footer"),
    "pageSetup": _rule(
        attributes="paperSize scale firstPageNumber fitToWidth fitToHeight pageOrder orientation usePrinterDefaults blackAndWhite draft cellComments useFirstPageNumber errors horizontalDpi verticalDpi copies"
    ),
    "headerFooter": _rule(
        "oddHeader oddFooter evenHeader evenFooter firstHeader firstFooter",
        "differentOddEven differentFirst scaleWithDoc alignWithMargins",
    ),
    "oddHeader": _rule(),
    "oddFooter": _rule(),
    "evenHeader": _rule(),
    "evenFooter": _rule(),
    "firstHeader": _rule(),
    "firstFooter": _rule(),
    "rowBreaks": _rule("brk", "count manualBreakCount"),
    "colBreaks": _rule("brk", "count manualBreakCount"),
    "brk": _rule(attributes="id min max man pt"),
    "ignoredErrors": _rule("ignoredError"),
    "ignoredError": _rule(
        attributes="sqref evalError twoDigitTextYear numberStoredAsText formula formulaRange unlockedFormula emptyCellReference listDataValidation calculatedColumn"
    ),
    "hyperlinks": _rule("hyperlink"),
    "hyperlink": (frozenset(), frozenset({"ref", "location", "tooltip", "display", R})),
    "tableParts": _rule("tablePart", "count"),
    "tablePart": (frozenset(), frozenset({R})),
    "workbook": _rule(
        "fileVersion workbookPr workbookProtection bookViews sheets definedNames calcPr"
    ),
    "fileVersion": _rule(attributes="appName lastEdited lowestEdited rupBuild codeName"),
    "workbookPr": _rule(
        attributes="date1904 showObjects showBorderUnselectedTables filterPrivacy promptedSolutions showInk backupFile saveExternalLinkValues updateLinks codeName hidePivotFieldList showPivotChartFilter allowRefreshQuery autoCompressPictures refreshAllConnections defaultThemeVersion"
    ),
    "workbookProtection": _rule(
        attributes="workbookPassword revisionsPassword lockStructure lockWindows lockRevision revisionsAlgorithmName revisionsHashValue revisionsSaltValue revisionsSpinCount workbookAlgorithmName workbookHashValue workbookSaltValue workbookSpinCount"
    ),
    "bookViews": _rule("workbookView"),
    "workbookView": _rule(
        attributes="visibility minimized showHorizontalScroll showVerticalScroll showSheetTabs xWindow yWindow windowWidth windowHeight tabRatio firstSheet activeTab autoFilterDateGrouping"
    ),
    "sheets": _rule("sheet"),
    "sheet": (frozenset(), frozenset({"name", "sheetId", "state", R})),
    "definedNames": _rule("definedName"),
    "definedName": _rule(
        attributes="name comment customMenu description help statusBar localSheetId hidden function vbProcedure xlm functionGroupId shortcutKey publishToServer workbookParameter"
    ),
    "calcPr": _rule(
        attributes="calcId calcMode fullCalcOnLoad refMode iterate iterateCount iterateDelta fullPrecision calcCompleted calcOnSave concurrentCalc concurrentManualCount forceFullCalc"
    ),
    "sst": _rule("si", "count uniqueCount"),
    "styleSheet": _rule(
        "numFmts fonts fills borders cellStyleXfs cellXfs cellStyles dxfs tableStyles colors"
    ),
    "numFmts": _rule("numFmt", "count"),
    "numFmt": _rule(attributes="numFmtId formatCode"),
    "fonts": _rule("font", "count"),
    "font": _rule(
        "name charset family b i strike outline shadow condense extend color sz u vertAlign scheme"
    ),
    "name": _rule(attributes="val"),
    "fills": _rule("fill", "count"),
    "fill": _rule("patternFill gradientFill"),
    "patternFill": _rule("fgColor bgColor", "patternType"),
    "fgColor": _rule(attributes="auto indexed rgb theme tint"),
    "bgColor": _rule(attributes="auto indexed rgb theme tint"),
    "gradientFill": _rule("stop", "type degree left right top bottom"),
    "stop": _rule("color", "position"),
    "borders": _rule("border", "count"),
    "border": _rule(
        "start end left right top bottom diagonal vertical horizontal",
        "diagonalUp diagonalDown outline",
    ),
    "left": _rule("color", "style"),
    "right": _rule("color", "style"),
    "top": _rule("color", "style"),
    "bottom": _rule("color", "style"),
    "start": _rule("color", "style"),
    "end": _rule("color", "style"),
    "diagonal": _rule("color", "style"),
    "vertical": _rule("color", "style"),
    "horizontal": _rule("color", "style"),
    "cellStyleXfs": _rule("xf", "count"),
    "cellXfs": _rule("xf", "count"),
    "xf": _rule(
        "alignment protection",
        "numFmtId fontId fillId borderId xfId quotePrefix pivotButton applyNumberFormat applyFont applyFill applyBorder applyAlignment applyProtection",
    ),
    "alignment": _rule(
        attributes="horizontal vertical textRotation wrapText shrinkToFit indent relativeIndent justifyLastLine readingOrder"
    ),
    "protection": _rule(attributes="locked hidden"),
    "cellStyles": _rule("cellStyle", "count"),
    "cellStyle": _rule(attributes="name xfId builtinId iLevel hidden customBuiltin"),
    "dxfs": _rule("dxf", "count"),
    "dxf": _rule("font numFmt fill alignment border protection"),
    "tableStyles": _rule("tableStyle", "count defaultTableStyle defaultPivotStyle"),
    "tableStyle": _rule("tableStyleElement", "name pivot table count"),
    "tableStyleElement": _rule(attributes="type size dxfId"),
    "colors": _rule("indexedColors mruColors"),
    "indexedColors": _rule("rgbColor"),
    "mruColors": _rule("color"),
    "rgbColor": _rule(attributes="rgb"),
    "table": _rule(
        "autoFilter sortState tableColumns tableStyleInfo",
        "id name displayName comment ref tableType headerRowCount insertRow insertRowShift totalsRowCount totalsRowShown published headerRowDxfId dataDxfId totalsRowDxfId headerRowBorderDxfId tableBorderDxfId totalsRowBorderDxfId headerRowCellStyle dataCellStyle totalsRowCellStyle connectionId",
    ),
    "tableColumns": _rule("tableColumn", "count"),
    "tableColumn": _rule(
        "calculatedColumnFormula totalsRowFormula",
        "id uniqueName name totalsRowFunction totalsRowLabel headerRowDxfId dataDxfId totalsRowDxfId headerRowCellStyle dataCellStyle totalsRowCellStyle",
    ),
    "calculatedColumnFormula": _rule(attributes="array"),
    "totalsRowFormula": _rule(attributes="array"),
    "tableStyleInfo": _rule(
        attributes="name showFirstColumn showLastColumn showRowStripes showColumnStripes"
    ),
}
_TEXT = frozenset(
    "f v t definedName oddHeader oddFooter evenHeader evenFooter firstHeader firstFooter calculatedColumnFormula totalsRowFormula".split()
)
# Native identity is explicit even where the general XML standard permits defaults.
_REQUIRED = {
    "row": {"r"},
    "c": {"r"},
    "autoFilter": {"ref"},
    "brk": {"id"},
    "tablePart": {R},
    "sheet": {R},
}
_IGNORABLE = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable"


def validate_structure(
    root: etree._Element,
    *,
    coordinate: Callable[[str | None], tuple[int, int]],
    bounds: Callable[[str | None], tuple[int, int, int, int]],
) -> None:
    """Validate only the closed supported profile, including nested inert metadata."""
    for element in root.iter():
        tag = etree.QName(element).localname
        rule = _RULES.get(tag)
        if rule is None or element.tag != S + tag:
            raise AdapterError(f"XLSX contains unsupported nested element {tag!r}")
        children, attributes = rule
        actual = set(element.attrib)
        # mc:Ignorable is an inert declaration; actual unknown extensions still fail.
        actual.discard(_IGNORABLE)
        required = set(REQUIRED_ATTRIBUTES.get(tag, ())) | _REQUIRED.get(tag, set())
        if actual - attributes or not required <= actual:
            raise AdapterError(f"XLSX {tag} has missing or unsupported attributes")
        if tag not in _TEXT and (element.text or "").strip():
            raise AdapterError(f"XLSX {tag} contains unsupported mixed text")
        _validate_children(element, tag, children)
        if element.get("count") is not None and tag != "sst":
            if _unsigned(element.get("count"), "count") != len(element):
                raise AdapterError(f"XLSX {tag} count does not match its children")
        for attribute, value in element.attrib.items():
            _validate_attribute(tag, attribute, value)
            if attribute in {"ref", "syncRef"}:
                bounds(value)
            elif attribute in {"activeCell", "topLeftCell"} or (tag == "c" and attribute == "r"):
                coordinate(value)
            elif attribute == "sqref":
                if not value.split():
                    raise AdapterError("XLSX sqref cannot be empty")
                for reference in value.split():
                    bounds(reference)
        if tag == "sheetFormatPr" and element.get("zeroHeight") in {"1", "true"}:
            raise AdapterError(
                "XLSX default-hidden rows (zeroHeight) are unsupported; explicit hidden rows are supported"
            )
        if tag == "autoFilter":
            rectangle = bounds(element.get("ref"))
            ids: set[int] = set()
            for column in element.findall(S + "filterColumn"):
                index = _unsigned(column.get("colId"), "filter column index")
                if index > rectangle[3] - rectangle[2] or index in ids:
                    raise AdapterError("XLSX auto-filter column index is invalid or duplicated")
                ids.add(index)
        if tag == "dateGroupItem":
            for attribute, (minimum, maximum) in {
                "year": (1, 9999),
                "month": (1, 12),
                "day": (1, 31),
                "hour": (0, 23),
                "minute": (0, 59),
                "second": (0, 59),
            }.items():
                if (
                    element.get(attribute) is not None
                    and not minimum <= _unsigned(element.get(attribute), attribute) <= maximum
                ):
                    raise AdapterError(f"XLSX date-group {attribute} is outside its bounds")


def _validate_children(element: etree._Element, tag: str, allowed: frozenset[str]) -> None:
    names = []
    allowed_tags = {S + name for name in allowed}
    for child in element:
        if child.tag not in allowed_tags or (child.tail or "").strip():
            raise AdapterError(f"XLSX {tag} contains unsupported nested content")
        names.append(etree.QName(child).localname)
    counts = Counter(names)
    for child, (minimum, maximum) in CHILD_COUNTS.get(tag, {}).items():
        if counts[child] < minimum or (maximum is not None and counts[child] > maximum):
            raise AdapterError(f"XLSX {tag}.{child} violates required child cardinality")
    order = CHILD_ORDER.get(tag)
    if order is not None:
        positions = [order.index(name) for name in names]
        if any(left > right for left, right in pairwise(positions)):
            raise AdapterError(f"XLSX {tag} has invalid child order")
    if tag in {"filterColumn", "fill"} and len(element) > 1:
        raise AdapterError(f"XLSX {tag} has conflicting metadata representations")


def _validate_attribute(tag: str, attribute: str, value: str) -> None:
    if len(value) > 8192:
        raise AdapterError("XLSX metadata attribute exceeds its character limit")
    key = (tag, attribute)
    label = {
        ("c", "t"): "stored cell type",
        ("f", "t"): "formula type",
        ("sheet", "state"): "worksheet visibility state",
    }.get(key, f"{tag}.{attribute}")
    if attribute in {R, _IGNORABLE}:
        return
    if attribute == SPACE:
        if value not in {"default", "preserve"}:
            raise AdapterError("XLSX xml:space has an unsupported value")
        return
    kind = SIMPLE_TYPES.get(key)
    if kind == "boolean":
        if value not in {"0", "1", "false", "true"}:
            raise AdapterError(f"XLSX {label} requires a boolean")
    elif kind in {"unsignedInt", "unsignedShort", "unsignedByte"}:
        number = _unsigned(value, label)
        maximum = {"unsignedInt": 2**32 - 1, "unsignedShort": 65535, "unsignedByte": 255}[kind]
        if number > maximum:
            raise AdapterError(f"XLSX {label} exceeds its integer range")
    elif kind in {"int", "integer"}:
        if re.fullmatch(r"-?[0-9]{1,10}", value) is None:
            raise AdapterError(f"XLSX {label} requires a bounded integer")
        minimum, maximum = INTEGER_RANGES.get(key, (-(2**31), 2**31 - 1))
        if not minimum <= int(value) <= maximum:
            raise AdapterError(f"XLSX {label} exceeds its integer range")
    elif kind == "double":
        _decimal(value, label)
    elif kind == "hexBinary":
        if re.fullmatch(r"[0-9a-fA-F]{" + str(HEX_LENGTHS[key] * 2) + r"}", value) is None:
            raise AdapterError(f"XLSX {label} requires fixed-length hexadecimal data")
    elif kind == "base64Binary":
        try:
            base64.b64decode("".join(value.split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AdapterError(f"XLSX {label} requires base64 data") from exc
    elif kind not in {"string", "token", "ST_Sqref", "ST_CellSpans"}:
        raise AdapterError(f"XLSX {label} has no supported validation rule")
    allowed_values = ENUMERATIONS.get(key)
    if allowed_values is not None and value not in allowed_values:
        raise AdapterError(f"XLSX {label} has an unsupported value")
    pattern = PATTERNS.get(key)
    if pattern is not None and re.fullmatch(pattern, value) is None:
        raise AdapterError(f"XLSX {label} does not match its required pattern")


def _unsigned(value: str | None, label: str) -> int:
    if value is None or re.fullmatch(r"[0-9]{1,10}", value) is None or int(value) > 2**32 - 1:
        raise AdapterError(f"XLSX {label} requires a bounded unsigned integer")
    return int(value)


def _decimal(value: str, label: str) -> None:
    try:
        valid = (
            re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value)
            is not None
            and Decimal(value).is_finite()
        )
    except InvalidOperation:
        valid = False
    if not valid:
        raise AdapterError(f"XLSX {label} requires a finite numeric value")
