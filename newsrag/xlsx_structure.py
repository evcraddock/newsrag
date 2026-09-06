"""Closed structural profile for the SpreadsheetML trees we accept.

OPC validates every part/namespace. This layer also validates nested content models
and inert attribute types, so an allowed metadata container cannot hide malformed
or unsupported workbook structures. It does not implement display formatting.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from lxml import etree  # type: ignore[import-untyped]

from newsrag.adapters import AdapterError
from newsrag.xlsx_package import REL_NS, XLSX_NS

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
_BOOL_VAL = frozenset("b i strike outline shadow condense extend".split())
_BOOLEAN = frozenset(
    "syncHorizontal syncVertical transitionEvaluation transitionEntry published filterMode enableFormatConditionsCalculation auto applyStyles summaryBelow summaryRight showOutlineSymbols autoPageBreaks fitToPage windowProtection showFormulas showGridLines showRowColHeaders showZeros rightToLeft tabSelected showRuler defaultGridColor showWhiteSpace customHeight zeroHeight thickTop thickBottom hidden bestFit customWidth phonetic collapsed customFormat thickBot ph aca ca bx sheet objects scenarios formatCells formatColumns formatRows insertColumns insertRows insertHyperlinks deleteColumns deleteRows selectLockedCells sort autoFilter pivotTables selectUnlockedCells hiddenButton showButton blank percent and cellColor columnSort caseSensitive descending horizontalCentered verticalCentered headings gridLines gridLinesSet usePrinterDefaults blackAndWhite draft useFirstPageNumber differentOddEven differentFirst scaleWithDoc alignWithMargins man pt evalError twoDigitTextYear numberStoredAsText formula formulaRange unlockedFormula emptyCellReference listDataValidation calculatedColumn date1904 showBorderUnselectedTables filterPrivacy promptedSolutions showInk backupFile saveExternalLinkValues hidePivotFieldList showPivotChartFilter allowRefreshQuery autoCompressPictures refreshAllConnections lockStructure lockWindows lockRevision minimized showHorizontalScroll showVerticalScroll showSheetTabs autoFilterDateGrouping function vbProcedure xlm publishToServer workbookParameter fullCalcOnLoad iterate fullPrecision calcCompleted calcOnSave concurrentCalc forceFullCalc diagonalUp diagonalDown outline quotePrefix pivotButton applyNumberFormat applyFont applyFill applyBorder applyAlignment applyProtection wrapText shrinkToFit justifyLastLine locked customBuiltin pivot table insertRow insertRowShift totalsRowShown array showFirstColumn showLastColumn showRowStripes showColumnStripes".split()
)
_UNSIGNED = frozenset(
    "indexed theme colorId zoomScale zoomScaleNormal zoomScaleSheetLayoutView zoomScalePageLayoutView workbookViewId activeCellId baseColWidth outlineLevelRow outlineLevelCol min max outlineLevel s cm vm si sb eb fontId spinCount colId year month day hour minute second dxfId iconId count paperSize scale firstPageNumber fitToWidth fitToHeight horizontalDpi verticalDpi copies manualBreakCount id defaultThemeVersion revisionsSpinCount workbookSpinCount windowWidth windowHeight tabRatio firstSheet activeTab sheetId localSheetId functionGroupId calcId iterateCount concurrentManualCount uniqueCount numFmtId fillId borderId xfId textRotation indent readingOrder builtinId iLevel size headerRowCount totalsRowCount headerRowDxfId dataDxfId totalsRowDxfId headerRowBorderDxfId tableBorderDxfId totalsRowBorderDxfId connectionId".split()
)
_DECIMAL = frozenset(
    "tint xSplit ySplit defaultColWidth defaultRowHeight width ht left right top bottom header footer filterVal maxVal iterateDelta degree position".split()
)
_REQUIRED = {
    "dimension": {"ref"},
    "sheetView": {"workbookViewId"},
    "sheetFormatPr": {"defaultRowHeight"},
    "col": {"min", "max"},
    "row": {"r"},
    "c": {"r"},
    "mergeCell": {"ref"},
    "hyperlink": {"ref"},
    "filterColumn": {"colId"},
    "autoFilter": {"ref"},
    "sortState": {"ref"},
    "sortCondition": {"ref"},
    "brk": {"id"},
    "ignoredError": {"sqref"},
    "tablePart": {R},
    "sheet": {"name", "sheetId", R},
    "pageMargins": {"left", "right", "top", "bottom", "header", "footer"},
}
_ENUMS = {
    ("sheetView", "view"): {"normal", "pageBreakPreview", "pageLayout"},
    ("pane", "state"): {"split", "frozen", "frozenSplit"},
    ("pane", "activePane"): {"bottomRight", "topRight", "bottomLeft", "topLeft"},
    ("selection", "pane"): {"bottomRight", "topRight", "bottomLeft", "topLeft"},
    ("pageSetup", "orientation"): {"default", "portrait", "landscape"},
    ("pageSetup", "pageOrder"): {"downThenOver", "overThenDown"},
    ("pageSetup", "cellComments"): {"none", "asDisplayed", "atEnd"},
    ("pageSetup", "errors"): {"displayed", "blank", "dash", "NA"},
    ("dateGroupItem", "dateTimeGrouping"): {"year", "month", "day", "hour", "minute", "second"},
    ("sortState", "sortMethod"): {"stroke", "pinYin", "none"},
    ("sortCondition", "sortBy"): {"value", "cellColor", "fontColor", "icon"},
    ("customFilter", "operator"): {
        "equal",
        "lessThan",
        "lessThanOrEqual",
        "notEqual",
        "greaterThanOrEqual",
        "greaterThan",
    },
    ("workbookView", "visibility"): {"visible", "hidden", "veryHidden"},
    ("calcPr", "calcMode"): {"manual", "auto", "autoNoTable"},
    ("calcPr", "refMode"): {"A1", "R1C1"},
    ("u", "val"): {"single", "double", "singleAccounting", "doubleAccounting", "none"},
    ("vertAlign", "val"): {"baseline", "superscript", "subscript"},
    ("scheme", "val"): {"major", "minor", "none"},
    ("t", SPACE): {"default", "preserve"},
}


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
        actual.discard("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable")
        if actual - attributes or not _REQUIRED.get(tag, set()) <= actual:
            raise AdapterError(f"XLSX {tag} has missing or unsupported attributes")
        if tag not in _TEXT and (element.text or "").strip():
            raise AdapterError(f"XLSX {tag} contains unsupported mixed text")
        for child in element:
            if child.tag not in {S + name for name in children} or (child.tail or "").strip():
                raise AdapterError(f"XLSX {tag} contains unsupported nested content")
        if element.get("count") is not None and tag != "sst":
            if _unsigned(element.get("count"), "count") != len(element):
                raise AdapterError(f"XLSX {tag} count does not match its children")
        if tag in {
            "sheetPr",
            "sheetView",
            "sheetFormatPr",
            "headerFooter",
            "xf",
            "dxf",
            "colors",
            "gradientFill",
            "autoFilter",
        }:
            repeatable = {"selection", "stop", "filterColumn"}
            tags = [
                child.tag for child in element if etree.QName(child).localname not in repeatable
            ]
            if len(tags) != len(set(tags)):
                raise AdapterError(f"XLSX {tag} duplicates singleton metadata")
        if tag in {"filterColumn", "fill"} and len(element) > 1:
            raise AdapterError(f"XLSX {tag} has conflicting metadata representations")
        if tag == "customFilters" and len(element) > 2:
            raise AdapterError("XLSX customFilters exceeds its two-filter limit")
        for attribute, value in element.attrib.items():
            if len(value) > 8192:
                raise AdapterError("XLSX metadata attribute exceeds its character limit")
            if (
                attribute in _BOOLEAN
                or (attribute == "val" and tag in _BOOL_VAL)
                or (tag == "top10" and attribute == "top")
            ):
                if value not in {"0", "1", "false", "true"}:
                    raise AdapterError(f"XLSX {tag}.{attribute} requires a boolean")
            elif (
                attribute in _UNSIGNED
                or (attribute == "style" and tag == "col")
                or (attribute == "r" and tag == "row")
                or (attribute == "val" and tag in {"family", "charset"})
            ):
                _unsigned(value, f"{tag}.{attribute}")
            elif attribute in _DECIMAL or (
                attribute == "val" and tag in {"sz", "top10", "dynamicFilter"}
            ):
                _decimal(value, f"{tag}.{attribute}")
            elif attribute in {"xWindow", "yWindow", "relativeIndent"}:
                if re.fullmatch(r"-?[0-9]{1,10}", value) is None:
                    raise AdapterError(f"XLSX {tag}.{attribute} requires an integer")
            if attribute in {"ref", "syncRef"}:
                bounds(value)
            elif attribute in {"activeCell", "topLeftCell"} or (tag == "c" and attribute == "r"):
                coordinate(value)
            elif attribute == "sqref":
                if not value.split():
                    raise AdapterError("XLSX sqref cannot be empty")
                for reference in value.split():
                    bounds(reference)
            allowed_values = _ENUMS.get((tag, attribute))
            if allowed_values is not None and value not in allowed_values:
                raise AdapterError(f"XLSX {tag}.{attribute} has an unsupported value")
            if attribute == "rgb" and re.fullmatch(r"[0-9A-Fa-f]{8}", value) is None:
                raise AdapterError("XLSX color requires an eight-digit ARGB value")
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
