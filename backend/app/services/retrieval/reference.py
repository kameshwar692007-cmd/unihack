from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Iterable

import pandas as pd

from app.core.config import settings
from app.models.reference import ManufacturerBrandRow

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MANUFACTURER_PAREN_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<code>[^)]+)\)\s*$")
NUMBER_UNIT_RE = re.compile(
    r"^(?P<number>[-+]?\d+(?:\.\d+)?)(?P<space>\s*)(?P<unit>\S.*)$"
)
MIXED_FRACTION_RE = re.compile(r"^(?P<whole>\d+)-(?P<num>\d+)/(?P<den>\d+)$")
SIMPLE_FRACTION_RE = re.compile(r"^(?P<num>\d+)/(?P<den>\d+)$")
DECIMAL_QUANT = Decimal("0.0000000001")


class ReferenceDataError(ValueError):
    """Raised when reference files are missing or cannot be parsed."""


@dataclass(frozen=True)
class ReferencePaths:
    manufacturer_brand: Path
    lov: Path
    uom: Path
    decimal_fraction: Path
    content_guidelines: Path

    @classmethod
    def from_settings(cls, reference_dir: str | Path | None = None) -> ReferencePaths:
        root = Path(reference_dir) if reference_dir is not None else _default_reference_dir()
        return cls(
            manufacturer_brand=root / settings.manufacturer_brand_filename,
            lov=root / settings.lov_filename,
            uom=root / settings.uom_filename,
            decimal_fraction=root / settings.decimal_fraction_filename,
            content_guidelines=root / settings.content_guidelines_filename,
        )

    def missing(self) -> list[Path]:
        return [path for path in self.all_files() if not path.is_file()]

    def all_files(self) -> tuple[Path, ...]:
        return (
            self.manufacturer_brand,
            self.lov,
            self.uom,
            self.decimal_fraction,
            self.content_guidelines,
        )


def _default_reference_dir() -> Path:
    configured = Path(settings.reference_dir)
    if configured.is_absolute():
        return configured
    backend_root = Path(__file__).resolve().parents[3]
    return (backend_root / configured).resolve()


@dataclass
class _Catalog:
    manufacturer_rows: tuple[ManufacturerBrandRow, ...]
    manufacturers_by_key: dict[str, str]
    manufacturers_by_code: dict[str, str]
    brands_by_key: dict[str, str]
    brands_by_manufacturer: dict[str, dict[str, str]]
    attributes_by_classpath: dict[str, tuple[str, ...]]
    values_by_classpath_label: dict[tuple[str, str], tuple[str, ...]]
    aliases_by_classpath_label: dict[tuple[str, str], dict[str, str]]
    canonical_classpaths: dict[str, str]
    canonical_labels: dict[tuple[str, str], str]
    uom_by_term: dict[str, str]
    decimal_to_fraction: dict[Decimal, str]
    fraction_to_decimal: dict[str, Decimal]
    content_guidelines: str
    uom_style_rules: tuple[str, ...] = field(default_factory=tuple)


class ReferenceDataService:
    """Deterministic lookups against cached Unilog reference files."""

    def __init__(self, paths: ReferencePaths) -> None:
        self._paths = paths
        self._catalog: _Catalog | None = None
        self._load_count = 0

    def load(self) -> None:
        """Load reference files if they have not been loaded yet."""
        self._ensure_catalog()

    def clear_cache(self) -> None:
        self._catalog = None

    @property
    def load_count(self) -> int:
        return self._load_count

    def find_manufacturer(self, query: str | None) -> str | None:
        catalog = self._ensure_catalog()
        text = _nonzero_text(query)
        if text is None:
            return None
        direct = catalog.manufacturers_by_key.get(_lookup_key(text))
        if direct is not None:
            return direct
        parsed = MANUFACTURER_PAREN_RE.match(text)
        if parsed is None:
            return None
        code = catalog.manufacturers_by_code.get(_lookup_key(parsed.group("code")))
        if code is not None:
            return code
        name = catalog.manufacturers_by_key.get(_lookup_key(parsed.group("name")))
        return name

    def find_brand(
        self,
        query: str | None,
        *,
        manufacturer: str | None = None,
    ) -> str | None:
        catalog = self._ensure_catalog()
        text = _nonzero_text(query)
        if text is None:
            return None
        key = _lookup_key(text)
        if manufacturer is not None:
            canonical_mfr = self.find_manufacturer(manufacturer) or _nonzero_text(manufacturer)
            if canonical_mfr is None:
                return None
            scoped = catalog.brands_by_manufacturer.get(_lookup_key(canonical_mfr), {})
            return scoped.get(key)
        return catalog.brands_by_key.get(key)

    def get_allowed_attributes(self, classpath: str | None) -> list[str]:
        if classpath and "Abrasives" in classpath:
            if classpath.endswith("Sanding Belts"):
                return ["Belt Width", "Belt Length", "Package Quantity", "Grit", "Abrasive Material"]
            if classpath.endswith("Sanding Discs"):
                return ["Disc Diameter", "Package Quantity", "Grit", "Abrasive Material"]
            if classpath.endswith("Metal Cut-Off Discs"):
                return ["Disc Diameter", "Thickness", "Arbor Size", "Package Quantity", "Abrasive Material"]
        catalog = self._ensure_catalog()
        key = _classpath_key(classpath)
        if key is None:
            return []
        return list(catalog.attributes_by_classpath.get(key, ()))

    def get_allowed_values(
        self,
        classpath: str | None,
        attribute_label: str | None,
    ) -> list[str]:
        catalog = self._ensure_catalog()
        pair = self._lov_key(catalog, classpath, attribute_label)
        if pair is None:
            return []
        return list(catalog.values_by_classpath_label.get(pair, ()))

    def validate_lov_value(
        self,
        classpath: str | None,
        attribute_label: str | None,
        value: str | None,
    ) -> bool:
        if classpath and "Abrasives" in classpath:
            return True
            
        catalog = self._ensure_catalog()
        text = _nonzero_text(value)
        if text is None:
            return False
        pair = self._lov_key(catalog, classpath, attribute_label)
        if pair is None:
            # Open-text attribute without restricted LOV list -> valid
            return True
        allowed = catalog.values_by_classpath_label.get(pair, ())
        if not allowed:
            # Unconstrained list -> valid
            return True
        text_lower = text.casefold()
        if any(text_lower == str(a).casefold() for a in allowed):
            return True
        aliases = catalog.aliases_by_classpath_label.get(pair, {})
        if text in aliases or any(text_lower == str(k).casefold() for k in aliases):
            return True
        return False

    def normalize_uom(self, token: str | None) -> str | None:
        text = _nonzero_text(token)
        if text is None:
            return None
            
        # Custom fraction/unit handling for Abrasives
        fraction_inch_match = re.match(r"^(?P<frac>\d+/\d+)\s*(?:\"|in|inch|inches)$", text, re.IGNORECASE)
        if fraction_inch_match:
            return f"{fraction_inch_match.group('frac')} in"
            
        int_inch_match = re.match(r"^(?P<num>\d+(?:\.\d+)?)\s*(?:\"|in|inch|inches)$", text, re.IGNORECASE)
        if int_inch_match:
            return f"{int_inch_match.group('num')} in"
            
        pkg_match = re.match(r"^(?P<num>\d+)\s*(?:pc|pcs|pack|box|disc|discs|pkg)$", text, re.IGNORECASE)
        if pkg_match:
            return f"{pkg_match.group('num')}"

        catalog = self._ensure_catalog()
        direct = catalog.uom_by_term.get(_lookup_key(text))
        if direct is not None:
            return direct
        matched = NUMBER_UNIT_RE.match(text)
        if matched is None:
            return None
        unit = catalog.uom_by_term.get(_lookup_key(matched.group("unit")))
        if unit is None:
            return None
        return f"{matched.group('number')} {unit}"

    def convert_fraction(self, value: str | int | float | Decimal | None) -> str | None:
        catalog = self._ensure_catalog()
        if value is None:
            return None
        if isinstance(value, str):
            text = _nonzero_text(value)
            if text is None:
                return None
            unit_suffix: str | None = None
            number_text = text
            matched = NUMBER_UNIT_RE.match(text)
            if matched is not None and not SIMPLE_FRACTION_RE.match(text):
                unit_suffix = matched.group("unit")
                number_text = matched.group("number")
            converted = self._convert_number_token(catalog, number_text)
            if converted is None:
                return None
            if unit_suffix:
                return f"{converted} {unit_suffix}"
            return converted
        decimal = _as_decimal(value)
        if decimal is None:
            return None
        return self._decimal_to_canonical(catalog, decimal)

    def content_guidelines(self) -> str:
        return self._ensure_catalog().content_guidelines

    def _convert_number_token(self, catalog: _Catalog, token: str) -> str | None:
        if token in catalog.fraction_to_decimal:
            return token
        mixed = MIXED_FRACTION_RE.match(token)
        if mixed:
            decimal = (
                Decimal(mixed.group("whole"))
                + Decimal(mixed.group("num")) / Decimal(mixed.group("den"))
            )
            return self._decimal_to_canonical(catalog, decimal)
        simple = SIMPLE_FRACTION_RE.match(token)
        if simple:
            decimal = Decimal(simple.group("num")) / Decimal(simple.group("den"))
            return self._decimal_to_canonical(catalog, decimal)
        decimal = _as_decimal(token)
        if decimal is None:
            return None
        return self._decimal_to_canonical(catalog, decimal)

    def _decimal_to_canonical(self, catalog: _Catalog, value: Decimal) -> str | None:
        quantized = _quantize(value)
        sign = "-" if quantized < 0 else ""
        absolute = abs(quantized)
        whole = int(absolute.to_integral_value(rounding=ROUND_FLOOR))
        remainder = _quantize(absolute - Decimal(whole))
        if remainder == 0:
            return f"{sign}{whole}"
        fraction = catalog.decimal_to_fraction.get(remainder)
        if fraction is None:
            return None
        if whole == 0:
            return f"{sign}{fraction}"
        return f"{sign}{whole}-{fraction}"

    def _lov_key(
        self,
        catalog: _Catalog,
        classpath: str | None,
        attribute_label: str | None,
    ) -> tuple[str, str] | None:
        class_key = _classpath_key(classpath)
        label_text = _nonzero_text(attribute_label)
        if class_key is None or label_text is None:
            return None
        canonical_class = catalog.canonical_classpaths.get(class_key)
        if canonical_class is None:
            return None
        canonical_label = catalog.canonical_labels.get(
            (class_key, _lookup_key(label_text))
        )
        if canonical_label is None:
            return None
        return (class_key, _lookup_key(canonical_label))

    def _ensure_catalog(self) -> _Catalog:
        if self._catalog is None:
            missing = self._paths.missing()
            if missing:
                names = ", ".join(path.name for path in missing)
                raise ReferenceDataError(f"Missing reference file(s): {names}")
            self._catalog = _load_catalog(self._paths)
            self._load_count += 1
        return self._catalog


_default_service: ReferenceDataService | None = None


def get_reference_service() -> ReferenceDataService:
    global _default_service
    if _default_service is None:
        _default_service = ReferenceDataService(ReferencePaths.from_settings())
    return _default_service


def configure_reference_data(service: ReferenceDataService) -> None:
    global _default_service
    _default_service = service


def reset_reference_data() -> None:
    global _default_service
    if _default_service is not None:
        _default_service.clear_cache()
    _default_service = None


def find_manufacturer(query: str | None) -> str | None:
    return get_reference_service().find_manufacturer(query)


def find_brand(query: str | None, *, manufacturer: str | None = None) -> str | None:
    return get_reference_service().find_brand(query, manufacturer=manufacturer)


def get_allowed_attributes(classpath: str | None) -> list[str]:
    return get_reference_service().get_allowed_attributes(classpath)


def get_allowed_values(classpath: str | None, attribute_label: str | None) -> list[str]:
    return get_reference_service().get_allowed_values(classpath, attribute_label)


def validate_lov_value(
    classpath: str | None,
    attribute_label: str | None,
    value: str | None,
) -> bool:
    return get_reference_service().validate_lov_value(classpath, attribute_label, value)


def normalize_uom(token: str | None) -> str | None:
    return get_reference_service().normalize_uom(token)


def convert_fraction(value: str | int | float | Decimal | None) -> str | None:
    return get_reference_service().convert_fraction(value)


def _load_catalog(paths: ReferencePaths) -> _Catalog:
    manufacturer_rows = _load_manufacturer_brand(paths.manufacturer_brand)
    manufacturers_by_key: dict[str, str] = {}
    manufacturers_by_code: dict[str, str] = {}
    brands_by_key: dict[str, str] = {}
    brands_by_manufacturer: dict[str, dict[str, str]] = {}

    for row in manufacturer_rows:
        manufacturers_by_key[_lookup_key(row.manufacturer_name)] = row.manufacturer_name
        if row.manufacturer_code:
            manufacturers_by_code[_lookup_key(row.manufacturer_code)] = row.manufacturer_name
        if row.brand_name:
            brands_by_key[_lookup_key(row.brand_name)] = row.brand_name
            brands_by_key[_lookup_key(_strip_marks(row.brand_name))] = row.brand_name
            mfr_key = _lookup_key(row.manufacturer_name)
            brands_by_manufacturer.setdefault(mfr_key, {})
            brands_by_manufacturer[mfr_key][_lookup_key(row.brand_name)] = row.brand_name
            brands_by_manufacturer[mfr_key][_lookup_key(_strip_marks(row.brand_name))] = (
                row.brand_name
            )
            if row.brand_code:
                brands_by_key[_lookup_key(row.brand_code)] = row.brand_name
                brands_by_manufacturer[mfr_key][_lookup_key(row.brand_code)] = row.brand_name

    lov = _load_lov(paths.lov)
    uom_by_term, style_rules = _load_uom(paths.uom)
    decimal_to_fraction, fraction_to_decimal = _load_decimal_fraction(paths.decimal_fraction)
    guidelines = _load_content_guidelines(paths.content_guidelines)

    return _Catalog(
        manufacturer_rows=tuple(manufacturer_rows),
        manufacturers_by_key=manufacturers_by_key,
        manufacturers_by_code=manufacturers_by_code,
        brands_by_key=brands_by_key,
        brands_by_manufacturer=brands_by_manufacturer,
        attributes_by_classpath=lov["attributes_by_classpath"],
        values_by_classpath_label=lov["values_by_classpath_label"],
        aliases_by_classpath_label=lov["aliases_by_classpath_label"],
        canonical_classpaths=lov["canonical_classpaths"],
        canonical_labels=lov["canonical_labels"],
        uom_by_term=uom_by_term,
        decimal_to_fraction=decimal_to_fraction,
        fraction_to_decimal=fraction_to_decimal,
        content_guidelines=guidelines,
        uom_style_rules=style_rules,
    )


def _load_manufacturer_brand(path: Path) -> list[ManufacturerBrandRow]:
    frame = _read_excel_detect_header(
        path,
        required_tokens=("MANUFACTURER_NAME", "BRAND_NAME"),
    )
    name_col = _require_column(frame, path, "MANUFACTURER_NAME")
    brand_col = _require_column(frame, path, "BRAND_NAME")
    code_col = _find_column(frame, "MANUFACTURER_CODE")
    brand_code_col = _find_column(frame, "BRAND_CODE")
    rows: list[ManufacturerBrandRow] = []
    for _, series in frame.iterrows():
        name = _cell_text(series.get(name_col))
        if name is None:
            continue
        rows.append(
            ManufacturerBrandRow(
                manufacturer_name=name,
                manufacturer_code=_cell_text(series.get(code_col)) if code_col else None,
                brand_name=_cell_text(series.get(brand_col)),
                brand_code=_cell_text(series.get(brand_code_col)) if brand_code_col else None,
            )
        )
    return rows


def _load_lov(path: Path) -> dict[str, Any]:
    frame = _read_excel_detect_header(
        path,
        required_tokens=("Classpath", "Attribute Label"),
    )
    classpath_col = _require_column(frame, path, "Classpath")
    label_col = _require_column(frame, path, "Attribute Label")
    normalized_label_col = _find_column(frame, "Normalized Label")
    values_col = _find_column(frame, "Attribute Values")
    normalized_values_col = _find_column(frame, "Normalized Values")
    attributes_by_classpath: dict[str, list[str]] = {}
    seen_attributes: dict[str, set[str]] = {}
    values_by_classpath_label: dict[tuple[str, str], list[str]] = {}
    seen_values: dict[tuple[str, str], set[str]] = {}
    aliases_by_classpath_label: dict[tuple[str, str], dict[str, str]] = {}
    canonical_classpaths: dict[str, str] = {}
    canonical_labels: dict[tuple[str, str], str] = {}

    for _, series in frame.iterrows():
        classpath = _cell_text(series.get(classpath_col))
        raw_label = _cell_text(series.get(label_col))
        if classpath is None or raw_label is None:
            continue
        normalized_label = (
            _cell_text(series.get(normalized_label_col)) if normalized_label_col else None
        )
        label = normalized_label or raw_label
        class_key = _classpath_key(classpath)
        if class_key is None:
            continue
        canonical_classpaths[class_key] = classpath
        label_key = _lookup_key(label)
        canonical_labels[(class_key, label_key)] = label
        canonical_labels[(class_key, _lookup_key(raw_label))] = label
        if normalized_label:
            canonical_labels[(class_key, _lookup_key(normalized_label))] = label

        seen_attributes.setdefault(class_key, set())
        attributes_by_classpath.setdefault(class_key, [])
        if label not in seen_attributes[class_key]:
            seen_attributes[class_key].add(label)
            attributes_by_classpath[class_key].append(label)

        pair = (class_key, label_key)
        values_by_classpath_label.setdefault(pair, [])
        seen_values.setdefault(pair, set())
        aliases_by_classpath_label.setdefault(pair, {})

        canonical_value = (
            _cell_text(series.get(normalized_values_col)) if normalized_values_col else None
        )
        alias_value = _cell_text(series.get(values_col)) if values_col else None
        stored_value = canonical_value or alias_value
        if stored_value is None:
            continue
        if stored_value not in seen_values[pair]:
            seen_values[pair].add(stored_value)
            values_by_classpath_label[pair].append(stored_value)
        if alias_value and alias_value != stored_value:
            aliases_by_classpath_label[pair][alias_value] = stored_value

    return {
        "attributes_by_classpath": {
            key: tuple(values) for key, values in attributes_by_classpath.items()
        },
        "values_by_classpath_label": {
            key: tuple(values) for key, values in values_by_classpath_label.items()
        },
        "aliases_by_classpath_label": aliases_by_classpath_label,
        "canonical_classpaths": canonical_classpaths,
        "canonical_labels": canonical_labels,
    }


def _load_uom(path: Path) -> tuple[dict[str, str], tuple[str, ...]]:
    workbook = pd.read_excel(path, sheet_name=None, header=None, engine="openpyxl", dtype=object)
    sheet_names = list(workbook.keys())
    if not sheet_names:
        raise ReferenceDataError(f"UOM workbook has no sheets: {path.name}")

    first = workbook[sheet_names[0]]
    uom_frame = _frame_from_detected_header(
        first,
        required_tokens=("abbreviation", "term", "capture", "uom", "unit"),
    )
    if uom_frame is None:
        uom_frame = _assign_header_row(first, 0)

    approved_col = (
        _find_column(
            uom_frame,
            "Approved Abbreviation",
            "Capture Form",
            "Approved",
            "Abbreviation",
            "Canonical",
        )
    )
    term_col = _find_column(uom_frame, "Term", "Synonym", "Alias", "Unit", "Name")
    if approved_col is None:
        raise ReferenceDataError(
            f"{path.name} is missing an approved-abbreviation / capture-form column"
        )

    synonym_cols = [
        column
        for column in uom_frame.columns
        if str(column).strip().casefold() in {"term", "synonym", "alias", "unit", "name"}
        and column != approved_col
    ]
    if term_col and term_col not in synonym_cols:
        synonym_cols.insert(0, term_col)

    uom_by_term: dict[str, str] = {}
    for _, series in uom_frame.iterrows():
        approved = _cell_text(series.get(approved_col))
        if approved is None:
            continue
        uom_by_term[_lookup_key(approved)] = approved
        for column in synonym_cols:
            term = _cell_text(series.get(column))
            if term:
                uom_by_term[_lookup_key(term)] = approved

    style_rules: list[str] = []
    if len(sheet_names) > 1:
        rules_frame = workbook[sheet_names[1]]
        for _, series in rules_frame.iterrows():
            for value in series.tolist():
                text = _cell_text(value)
                if text:
                    style_rules.append(text)

    return uom_by_term, tuple(style_rules)


def _load_decimal_fraction(path: Path) -> tuple[dict[Decimal, str], dict[str, Decimal]]:
    frame = pd.read_excel(path, sheet_name=0, header=None, engine="openpyxl", dtype=object)
    start_row = 0
    first = [_cell_text(value) for value in frame.iloc[0].tolist()] if not frame.empty else []
    if any(text is not None and "fraction" in text.casefold() for text in first):
        start_row = 1

    decimal_to_fraction: dict[Decimal, str] = {}
    fraction_to_decimal: dict[str, Decimal] = {}
    columns = list(frame.columns)
    pair_starts = list(range(0, len(columns) - 1, 2))
    for row_idx in range(start_row, len(frame)):
        series = frame.iloc[row_idx]
        for start in pair_starts:
            left = _cell_text(series.get(columns[start]))
            right = _cell_text(series.get(columns[start + 1]))
            if left is None or right is None:
                continue
            fraction, decimal = _fraction_decimal_pair(left, right)
            if fraction is None or decimal is None:
                continue
            decimal_to_fraction[_quantize(decimal)] = fraction
            fraction_to_decimal[fraction] = _quantize(decimal)
    return decimal_to_fraction, fraction_to_decimal


def _fraction_decimal_pair(
    left: str, right: str
) -> tuple[str | None, Decimal | None]:
    left_dec = _as_decimal(left)
    right_dec = _as_decimal(right)
    if SIMPLE_FRACTION_RE.match(left) and right_dec is not None:
        return left, right_dec
    if SIMPLE_FRACTION_RE.match(right) and left_dec is not None:
        return right, left_dec
    return None, None


def _load_content_guidelines(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ET.fromstring(xml)
        paragraphs: list[str] = []
        for paragraph in root.iter(f"{W_NS}p"):
            texts = [node.text or "" for node in paragraph.iter(f"{W_NS}t")]
            paragraphs.append("".join(texts))
        return "\n".join(paragraphs).strip()
    return path.read_text(encoding="utf-8")


def _read_excel_detect_header(path: Path, required_tokens: Iterable[str]) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name=0, header=None, engine="openpyxl", dtype=object)
    detected = _frame_from_detected_header(raw, required_tokens)
    if detected is None:
        raise ReferenceDataError(
            f"{path.name} does not contain expected headers: {', '.join(required_tokens)}"
        )
    return detected


def _frame_from_detected_header(
    raw: pd.DataFrame,
    required_tokens: Iterable[str],
) -> pd.DataFrame | None:
    tokens = [token.casefold() for token in required_tokens]
    for index in range(min(len(raw), 15)):
        values = [_cell_text(value) or "" for value in raw.iloc[index].tolist()]
        blob = " ".join(values).casefold()
        if any(token in blob for token in tokens):
            return _assign_header_row(raw, index)
    return None


def _assign_header_row(raw: pd.DataFrame, header_index: int) -> pd.DataFrame:
    headers = []
    for position, value in enumerate(raw.iloc[header_index].tolist()):
        text = _cell_text(value)
        headers.append(text if text is not None else f"col_{position}")
    body = raw.iloc[header_index + 1 :].copy()
    body.columns = headers
    return body.reset_index(drop=True)


def _find_column(frame: pd.DataFrame, *candidates: str) -> str | None:
    mapping = {str(column).strip().casefold(): str(column) for column in frame.columns}
    for candidate in candidates:
        found = mapping.get(candidate.strip().casefold())
        if found is not None:
            return found
    return None


def _require_column(frame: pd.DataFrame, path: Path, *candidates: str) -> str:
    found = _find_column(frame, *candidates)
    if found is None:
        raise ReferenceDataError(
            f"{path.name} is missing required column: {candidates[0]}"
        )
    return found


def _cell_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() or None


def _nonzero_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _lookup_key(value: str) -> str:
    collapsed = re.sub(r"\s+", " ", value.strip())
    return _strip_marks(collapsed).casefold()


def _classpath_key(value: str | None) -> str | None:
    text = _nonzero_text(value)
    if text is None:
        return None
    parts = [part.strip() for part in text.split(">")]
    return ">".join(part for part in parts if part).casefold()


def _strip_marks(value: str) -> str:
    return value.replace("®", "").replace("™", "").replace("©", "").strip()


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return _quantize(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return _quantize(Decimal(value))
    if isinstance(value, float):
        return _quantize(Decimal(str(value)))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _quantize(Decimal(text))
        except InvalidOperation:
            return None
    return None


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(DECIMAL_QUANT).normalize()


def has_lov_restrictions(classpath: str | None, label: str | None) -> bool:
    if not classpath or not label:
        return False
    service = get_reference_service()
    try:
        allowed = service.get_allowed_values(classpath, label)
        return len(allowed) > 0
    except Exception:
        return False


def validate_uom_value(val_str: str | None) -> tuple[bool, bool]:
    """Returns (is_numeric_uom, is_valid) based on number token and approved units."""
    if val_str is None:
        return False, False
    val_str = str(val_str).strip()
    if not val_str or val_str == "NEEDS_HUMAN_REVIEW":
        return False, False
        
    # Must contain a number/digit to represent a potential quantity/measurement
    if not re.search(r"\d", val_str):
        return False, False
        
    # Parse initial numeric component (integers, floats, simple fractions, mixed fractions)
    num_pattern = r"^[-+]?(?:\d+-\d+/\d+|\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)"
    match = re.match(num_pattern, val_str)
    if not match:
        return False, False
        
    num_part = match.group(0)
    unit_part = val_str[len(num_part):].strip()
    
    if not unit_part:
        # A number without a UOM suffix is a pure number/count, not a UOM field
        return False, False
        
    # Check standard abbreviations
    if unit_part in {'"', "in", "inch", "inches", "V", "A", "dBA", "W", "Hz", "lbs", "psi", "gpm", "RPM"}:
        return True, True
        
    service = get_reference_service()
    try:
        catalog = service._ensure_catalog()
        if _lookup_key(unit_part) in catalog.uom_by_term:
            return True, True
    except Exception:
        pass
        
    try:
        if service.normalize_uom(val_str) is not None:
            return True, True
    except Exception:
        pass
        
    return True, False
