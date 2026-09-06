from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree  # type: ignore[import-untyped]
from test_xlsx_adapter import extract, package_with_parts, text_cell
from xlsx_fixtures import XLSX_NS

from newsrag.adapters import AdapterError
from newsrag.xlsx_adapter import _bounds, _coordinate
from newsrag.xlsx_structure import (
    _REQUIRED,
    _RULES,
    SPACE,
    R,
    S,
    _validate_attribute,
    validate_structure,
)
from newsrag.xlsx_structure_rules import (
    CHILD_COUNTS,
    CHILD_ORDER,
    ENUMERATIONS,
    REQUIRED_ATTRIBUTES,
    SIMPLE_TYPES,
)


def validate(element: etree._Element) -> None:
    validate_structure(element, coordinate=_coordinate, bounds=_bounds)


def element_with_required_attributes(tag: str) -> etree._Element:
    element = etree.Element(S + tag)
    for attribute in set(REQUIRED_ATTRIBUTES.get(tag, ())) | _REQUIRED.get(tag, set()):
        element.set(attribute, "1")
    return element


def test_closed_profile_has_rules_for_every_accepted_attribute_and_child() -> None:
    for tag, (children, attributes) in _RULES.items():
        assert children == CHILD_COUNTS.get(tag, {}).keys()
        assert all(
            attribute in {R, SPACE} or (tag, attribute) in SIMPLE_TYPES for attribute in attributes
        )
        assert set(CHILD_ORDER.get(tag, ())) <= children


@pytest.mark.parametrize("key", list(ENUMERATIONS), ids=lambda key: ".".join(key))
def test_every_enum_rejects_unknown_values_and_accepts_its_declared_values(
    key: tuple[str, str],
) -> None:
    with pytest.raises(AdapterError, match="unsupported value"):
        _validate_attribute(*key, "not-a-supported-enum")
    for value in ENUMERATIONS[key]:
        _validate_attribute(*key, value)


@pytest.mark.parametrize(
    "tag,attribute",
    [
        (tag, attribute)
        for tag, attributes in REQUIRED_ATTRIBUTES.items()
        for attribute in attributes
    ],
)
def test_every_required_attribute_is_enforced(tag: str, attribute: str) -> None:
    root = element_with_required_attributes(tag)
    del root.attrib[attribute]
    with pytest.raises(AdapterError, match="missing or unsupported attributes"):
        validate(root)


@pytest.mark.parametrize(
    "tag,child,maximum",
    [
        (tag, child, maximum)
        for tag, children in CHILD_COUNTS.items()
        for child, (_, maximum) in children.items()
        if maximum is not None
    ],
)
def test_every_finite_child_cardinality_is_enforced(tag: str, child: str, maximum: int) -> None:
    root = element_with_required_attributes(tag)
    for other, (minimum, _) in CHILD_COUNTS[tag].items():
        if other != child:
            for _ in range(minimum):
                etree.SubElement(root, S + other)
    for _ in range(maximum + 1):
        etree.SubElement(root, S + child)
    with pytest.raises(AdapterError, match=rf"{tag}\.{child}.*cardinality"):
        validate(root)


@pytest.mark.parametrize(
    "tag,child",
    [
        (tag, child)
        for tag, children in CHILD_COUNTS.items()
        for child, (minimum, _) in children.items()
        if minimum > 0
    ],
)
def test_every_required_child_is_enforced(tag: str, child: str) -> None:
    root = element_with_required_attributes(tag)
    for other, (minimum, _) in CHILD_COUNTS[tag].items():
        if other != child:
            for _ in range(minimum):
                etree.SubElement(root, S + other)
    assert root.find(S + child) is None
    with pytest.raises(AdapterError, match=rf"{tag}\.{child}.*cardinality"):
        validate(root)


@pytest.mark.parametrize(
    "tag,attribute,value",
    [
        ("row", "outlineLevel", "256"),
        ("charset", "val", str(2**31)),
        ("family", "val", "15"),
        ("sheetProtection", "password", "ABC"),
        ("sheetProtection", "hashValue", "not base64!"),
        ("fileVersion", "codeName", "not-a-guid"),
    ],
)
def test_metadata_scalar_constraints_are_not_inferred_from_attribute_names(
    tag: str, attribute: str, value: str
) -> None:
    with pytest.raises(AdapterError, match="XLSX"):
        _validate_attribute(tag, attribute, value)


@pytest.mark.parametrize(
    "case", ["dynamic-enum", "alignment-enum", "missing-date-fields", "duplicate-foreground"]
)
def test_reviewed_metadata_gaps_fail_through_the_actual_adapter(tmp_path: Path, case: str) -> None:
    after = ""
    styles = None
    if case == "dynamic-enum":
        after = '<autoFilter ref="A1"><filterColumn colId="0"><dynamicFilter type="notARealFilter"/></filterColumn></autoFilter>'
    elif case == "missing-date-fields":
        after = '<autoFilter ref="A1"><filterColumn colId="0"><filters><dateGroupItem/></filters></filterColumn></autoFilter>'
    elif case == "alignment-enum":
        styles = f'<styleSheet xmlns="{XLSX_NS}"><cellXfs count="1"><xf><alignment horizontal="sideways"/></xf></cellXfs></styleSheet>'
    else:
        styles = f'<styleSheet xmlns="{XLSX_NS}"><fills count="1"><fill><patternFill><fgColor rgb="FFFF0000"/><fgColor rgb="FF00FF00"/></patternFill></fill></fills></styleSheet>'
    path = package_with_parts(
        tmp_path, '<row r="1">' + text_cell("A1", "Visible") + "</row>", after=after, styles=styles
    )
    with pytest.raises(
        AdapterError, match="unsupported value|missing or unsupported attributes|cardinality"
    ):
        extract(path)


def test_valid_filter_and_date_metadata_do_not_filter_or_reformat_evidence(tmp_path: Path) -> None:
    after = '<autoFilter ref="A1:B1"><filterColumn colId="0"><dynamicFilter type="thisMonth"/></filterColumn><filterColumn colId="1"><filters><dateGroupItem year="2026" month="9" dateTimeGrouping="month"/></filters></filterColumn></autoFilter>'
    path = package_with_parts(
        tmp_path,
        '<row r="1">' + text_cell("A1", "Visible") + '<c r="B1"><v>0012.50</v></c></row>',
        after=after,
    )
    result = extract(path)
    assert result.tables[0].cells[0].value == "Visible"
    assert result.tables[0].cells[1].value == "0012.50"
