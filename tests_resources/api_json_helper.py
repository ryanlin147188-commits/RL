from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path
from typing import Any, List, Tuple, Union

from robot.api.deco import keyword


JsonToken = Union[str, int]
_MISSING = object()
_LOCAL_JSON_SOURCES: dict[str, Any] = {}
_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?")
_COMMON_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%H:%M:%S",
    "%H:%M",
)


def _parse_json_path(json_path: str, allow_blank: bool = False) -> List[JsonToken]:
    if not json_path or not json_path.strip():
        if allow_blank:
            return []
        raise ValueError("JSON 路徑不可為空白。")

    tokens: List[JsonToken] = []
    token_pattern = re.compile(r"([^.\[\]]+)|\[(\d+)\]")
    for match in token_pattern.finditer(json_path):
        key_name, list_index = match.groups()
        if key_name is not None:
            tokens.append(key_name)
        else:
            tokens.append(int(list_index))

    if not tokens:
        raise ValueError(f"無法解析 JSON 路徑: {json_path}")

    return tokens


def _resolve_json_path(data: Any, json_path: str, allow_blank: bool = False) -> Any:
    current = data
    for token in _parse_json_path(json_path, allow_blank=allow_blank):
        current = current[token]
    return current


def _try_resolve_json_path(data: Any, json_path: str) -> Tuple[bool, Any]:
    try:
        return True, _resolve_json_path(data, json_path)
    except (KeyError, IndexError, TypeError):
        return False, _MISSING


def _is_empty_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {} or value == ()


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _compare_scalar_values(actual_value: Any, operator: str, expected: str) -> bool:
    if operator in (">", ">=", "<", "<="):
        actual_number = float(actual_value)
        expected_number = float(expected)
        if operator == ">":
            return actual_number > expected_number
        if operator == ">=":
            return actual_number >= expected_number
        if operator == "<":
            return actual_number < expected_number
        return actual_number <= expected_number

    actual = str(actual_value)
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    if operator == "contains":
        return expected in actual
    if operator == "not contains":
        return expected not in actual
    raise ValueError(f"不支援的比較運算子: {operator}")


def _parse_field_set(expected_fields: str) -> List[str]:
    fields = [field.strip() for field in (expected_fields or "").split(",") if field.strip()]
    if not fields:
        raise ValueError("欄位集合不可為空白。")
    return fields


def _parse_value_condition_expression(value_condition: str) -> Tuple[str, str]:
    normalized = (value_condition or "").strip()
    if not normalized:
        raise ValueError("值條件不可為空白。")

    prefixes = (
        ("not contains ", "not contains"),
        ("contains ", "contains"),
        (">= ", ">="),
        ("<= ", "<="),
        ("== ", "=="),
        ("!= ", "!="),
        ("> ", ">"),
        ("< ", "<"),
    )
    for prefix, operator in prefixes:
        if normalized.startswith(prefix):
            expected = normalized[len(prefix):].strip()
            if not expected:
                raise ValueError(f"值條件格式不正確: {value_condition}")
            return operator, expected

    raise ValueError(f"不支援的值條件: {value_condition}")


def _parse_query_options(query_options: str = "") -> Tuple[str, bool, int | None]:
    sort_by = ""
    descending = False
    top_n: int | None = None

    normalized = (query_options or "").strip()
    if not normalized:
        return sort_by, descending, top_n

    for part in normalized.split(";"):
        clause = part.strip()
        if not clause:
            continue
        if "=" not in clause:
            raise ValueError(f"query_options 格式不正確: {clause}")

        key, value = clause.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "sort_by":
            sort_by = value
        elif key in ("order", "sort_order"):
            descending = value.lower() in ("desc", "descending")
        elif key == "top_n":
            top_n = int(value)
            if top_n <= 0:
                raise ValueError("top_n 必須大於 0")
        else:
            raise ValueError(f"不支援的 query_options 參數: {key}")

    return sort_by, descending, top_n


def _normalize_sort_value(value: Any) -> Tuple[int, Any]:
    if value is None:
        return (2, "")
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    return (1, str(value))


def _apply_query_options(items: List[Any], query_options: str = "") -> List[Any]:
    sort_by, descending, top_n = _parse_query_options(query_options)
    processed = list(items)

    if sort_by:
        try:
            processed.sort(
                key=lambda item: _normalize_sort_value(_resolve_json_path(item, sort_by)),
                reverse=descending,
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise AssertionError(f"排序欄位 '{sort_by}' 無法解析") from exc

    if top_n is not None:
        processed = processed[:top_n]

    return processed


def _get_header_values(response: Any, header_name: str) -> List[str]:
    values: List[str] = []
    raw_headers = getattr(getattr(response, "raw", None), "headers", None)
    if raw_headers is not None:
        for method_name in ("getlist", "get_all"):
            method = getattr(raw_headers, method_name, None)
            if callable(method):
                result = method(header_name)
                if result:
                    if isinstance(result, str):
                        values = [result]
                    else:
                        values = [str(item) for item in result]
                    break

    if not values:
        header_value = response.headers.get(header_name)
        if header_value is None:
            return []
        values = [str(header_value)]

    return values


def _parse_condition_expression(condition_expression: str) -> Tuple[str, str, str]:
    normalized = (condition_expression or "").strip()
    if not normalized:
        raise ValueError("條件式不可為空白。")

    patterns = (
        (" not contains ", "not contains"),
        (" contains ", "contains"),
        (" == ", "=="),
        (" != ", "!="),
    )
    for separator, operator in patterns:
        if separator in normalized:
            path, expected = normalized.split(separator, 1)
            path = path.strip()
            expected = expected.strip()
            if not path or not expected:
                raise ValueError(f"條件式格式不正確: {condition_expression}")
            return path, operator, expected

    raise ValueError(f"不支援的條件式: {condition_expression}")


def _matches_condition(actual_value: Any, operator: str, expected: str) -> bool:
    return _compare_scalar_values(actual_value, operator, expected)


def _resolve_json_array(data: Any, array_path: str = "") -> List[Any]:
    current = _resolve_json_path(data, array_path, allow_blank=True)
    if not isinstance(current, list):
        raise AssertionError(f"JSON 路徑 '{array_path}' 對應的值不是陣列")
    return current


def _find_matching_items(data: Any, condition_expression: str, array_path: str = "") -> List[Any]:
    items = _resolve_json_array(data, array_path)
    condition_path, operator, expected = _parse_condition_expression(condition_expression)
    matches: List[Any] = []

    for item in items:
        exists, actual_value = _try_resolve_json_path(item, condition_path)
        if not exists:
            continue
        if _matches_condition(actual_value, operator, expected):
            matches.append(item)

    return matches


def _parse_schema_rules(schema_rules: str) -> Tuple[List[str], dict[str, str]]:
    required_fields: List[str] = []
    type_rules: dict[str, str] = {}

    for part in (schema_rules or "").split(";"):
        clause = part.strip()
        if not clause:
            continue

        if clause.startswith("required="):
            required_fields.extend(_parse_field_set(clause.split("=", 1)[1]))
            continue

        if clause.startswith("type="):
            type_rules[""] = clause.split("=", 1)[1].strip().lower()
            continue

        if clause.startswith("type."):
            left, expected_type = clause.split("=", 1)
            path = left[len("type.") :].strip()
            if not path:
                raise ValueError(f"Schema 規則格式不正確: {clause}")
            type_rules[path] = expected_type.strip().lower()
            continue

        raise ValueError(f"不支援的 Schema 規則: {clause}")

    if not required_fields and not type_rules:
        raise ValueError("Schema 規則不可為空白。")

    return required_fields, type_rules


def _stringify_json_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _normalize_text_value(value: Any) -> str:
    return re.sub(r"\s+", " ", _stringify_json_value(value)).strip()


def _resolve_local_json_file_path(file_path: str) -> Path:
    normalized = (file_path or "").strip()
    if not normalized:
        raise ValueError("JSON 檔案路徑不可為空白。")

    candidate = Path(normalized).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()

    if not candidate.exists():
        raise AssertionError(f"找不到 JSON 檔案: {file_path}")
    if not candidate.is_file():
        raise AssertionError(f"JSON 路徑不是檔案: {file_path}")
    return candidate


def _load_local_json_document(file_path: str) -> Any:
    resolved_path = _resolve_local_json_file_path(file_path)
    raw_content = resolved_path.read_text(encoding="utf-8-sig")

    try:
        if resolved_path.suffix.lower() == ".jsonl":
            return [json.loads(line) for line in raw_content.splitlines() if line.strip()]
        return json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"JSON 檔案格式不正確: {file_path}") from exc


def _get_local_json_source(source_alias: str) -> Any:
    alias = (source_alias or "").strip()
    if not alias:
        raise ValueError("JSON 資料來源別名不可為空白。")
    if alias not in _LOCAL_JSON_SOURCES:
        raise AssertionError(f"找不到已載入的 JSON 資料來源: {source_alias}")
    return _LOCAL_JSON_SOURCES[alias]


def _normalize_decimal_string(value: Any) -> str:
    raw_value = _normalize_text_value(value).replace(",", "")
    if raw_value == "":
        return ""

    try:
        decimal_value = Decimal(raw_value)
    except InvalidOperation as exc:
        raise AssertionError(f"無法將值轉成數字格式: {value}") from exc

    normalized = format(decimal_value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _extract_number_string(value: Any) -> str:
    text = _normalize_text_value(value)
    match = _NUMBER_PATTERN.search(text)
    if match is None:
        raise AssertionError(f"找不到可供擷取的數字: {value}")
    return _normalize_decimal_string(match.group(0))


def _parse_common_datetime(value: str) -> datetime | None:
    normalized = _normalize_text_value(value)
    if not normalized:
        return None

    for date_format in _COMMON_DATETIME_FORMATS:
        try:
            return datetime.strptime(normalized, date_format)
        except ValueError:
            continue
    return None


def _normalize_datetime_string(value: Any, target_type: str) -> str:
    normalized = _normalize_text_value(value)
    parsed = _parse_common_datetime(normalized)
    if parsed is None:
        raise AssertionError(f"無法將值轉成日期時間格式: {value}")

    if target_type == "date":
        return parsed.strftime("%Y-%m-%d")
    if target_type == "time":
        return parsed.strftime("%H:%M") if parsed.second == 0 else parsed.strftime("%H:%M:%S")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_boolean_string(value: Any, yes_value: str, no_value: str) -> str:
    if isinstance(value, bool):
        return yes_value if value else no_value

    normalized = _normalize_text_value(value).lower()
    if normalized in ("true", "1", "yes", "y", "on", "是"):
        return yes_value
    if normalized in ("false", "0", "no", "n", "off", "否"):
        return no_value
    raise AssertionError(f"無法將值轉成布林格式: {value}")


def _normalize_comparison_value(value: Any, normalizer: str = "") -> str:
    normalized = _normalize_text_value(value)
    if not normalizer or not normalizer.strip():
        return normalized

    tokens = [token.strip().lower() for token in re.split(r"[|,]", normalizer) if token.strip()]
    if not tokens:
        return normalized

    current: Any = normalized
    for token in tokens:
        if token in ("text", "strip", "trim", "collapse_spaces"):
            current = _normalize_text_value(current)
        elif token == "lower":
            current = _normalize_text_value(current).lower()
        elif token == "upper":
            current = _normalize_text_value(current).upper()
        elif token == "number":
            current = _normalize_decimal_string(current)
        elif token == "extract_number":
            current = _extract_number_string(current)
        elif token == "date":
            current = _normalize_datetime_string(current, "date")
        elif token == "time":
            current = _normalize_datetime_string(current, "time")
        elif token == "datetime":
            current = _normalize_datetime_string(current, "datetime")
        elif token == "bool_yes_no":
            current = _normalize_boolean_string(current, "是", "否")
        elif token == "bool_true_false":
            current = _normalize_boolean_string(current, "True", "False")
        else:
            raise AssertionError(f"不支援的 normalizer: {token}")

    return _normalize_text_value(current)


@keyword("重置本地JSON資料來源")
def reset_local_json_sources() -> None:
    _LOCAL_JSON_SOURCES.clear()


@keyword("載入本地JSON資料來源")
def load_local_json_source(source_alias: str, file_path: str) -> str:
    alias = (source_alias or "").strip()
    if not alias:
        raise ValueError("JSON 資料來源別名不可為空白。")

    _LOCAL_JSON_SOURCES[alias] = _load_local_json_document(file_path)
    return alias


@keyword("從本地JSON資料來源取得欄位值")
def get_json_value_from_local_source(source_alias: str, json_path: str = "") -> str:
    data = _get_local_json_source(source_alias)
    try:
        current = _resolve_json_path(data, json_path, allow_blank=True)
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionError(f"JSON 路徑 '{json_path}' 無法從資料來源 '{source_alias}' 解析") from exc
    return _stringify_json_value(current)


@keyword("從本地JSON資料來源依條件取得欄位值")
def get_json_array_field_value_by_condition_from_local_source(
    source_alias: str,
    condition_expression: str,
    target_path: str = "",
    array_path: str = "",
    query_options: str = "",
) -> str:
    data = _get_local_json_source(source_alias)
    try:
        matches = _find_matching_items(data, condition_expression, array_path)
        matches = _apply_query_options(matches, query_options)
    except (KeyError, IndexError, TypeError, AssertionError, ValueError) as exc:
        raise AssertionError(f"資料來源 '{source_alias}' 條件式查找失敗: {exc}") from exc

    if not matches:
        raise AssertionError(f"資料來源 '{source_alias}' 找不到符合條件的 JSON 陣列項目: {condition_expression}")

    try:
        current = _resolve_json_path(matches, target_path, allow_blank=True)
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionError(f"目標路徑 '{target_path}' 無法從資料來源 '{source_alias}' 取值") from exc
    return _stringify_json_value(current)


@keyword("從本地JSON資料來源取得結果數量")
def get_json_array_match_count_from_local_source(
    source_alias: str,
    array_path: str = "",
    condition_expression: str = "",
    query_options: str = "",
) -> str:
    data = _get_local_json_source(source_alias)

    try:
        if condition_expression and condition_expression.strip():
            matches = _find_matching_items(data, condition_expression, array_path)
        else:
            matches = _resolve_json_array(data, array_path)
        matches = _apply_query_options(matches, query_options)
    except (KeyError, IndexError, TypeError, AssertionError, ValueError) as exc:
        raise AssertionError(f"資料來源 '{source_alias}' 結果數量查找失敗: {exc}") from exc

    return str(len(matches))


@keyword("正規化比對值")
def normalize_comparison_value(value: Any, normalizer: str = "") -> str:
    return _normalize_comparison_value(value, normalizer)


@keyword("從回應取得HTTP Header值")
def get_http_header_value_from_response(response: Any, header_name: str) -> str:
    if not header_name or not header_name.strip():
        raise ValueError("HTTP Header 名稱不可為空白。")

    values = _get_header_values(response, header_name)
    if not values:
        raise AssertionError(f"找不到 HTTP Header: {header_name}")
    return ", ".join(values)


@keyword("從回應檢查HTTP Header是否存在")
def http_header_exists_in_response(response: Any, header_name: str) -> str:
    if not header_name or not header_name.strip():
        raise ValueError("HTTP Header 名稱不可為空白。")
    return str(bool(_get_header_values(response, header_name)))


@keyword("從回應取得JSON欄位值")
def get_json_value_from_response(response: Any, json_path: str) -> str:
    try:
        current = _resolve_json_path(response.json(), json_path)
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionError(f"JSON 路徑 '{json_path}' 無法解析") from exc
    return _stringify_json_value(current)


@keyword("從回應取得JSON欄位型別")
def get_json_field_type_from_response(response: Any, json_path: str = "") -> str:
    try:
        current = _resolve_json_path(response.json(), json_path, allow_blank=True)
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionError(f"JSON 路徑 '{json_path}' 無法解析型別") from exc
    return _json_type_name(current)


@keyword("從回應檢查JSON欄位型別加值")
def validate_json_field_type_and_value_in_response(
    response: Any,
    json_path: str,
    expected_type: str,
    value_condition: str,
) -> str:
    if not json_path or not json_path.strip():
        raise ValueError("JSON 路徑不可為空白。")
    if not expected_type or not expected_type.strip():
        raise ValueError("預期型別不可為空白。")
    if not value_condition or not value_condition.strip():
        raise ValueError("值條件不可為空白。")

    try:
        current = _resolve_json_path(response.json(), json_path)
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionError(f"JSON 路徑 '{json_path}' 無法解析型別與數值") from exc

    actual_type = _json_type_name(current)
    if actual_type != expected_type.strip().lower():
        return "False"

    try:
        operator, expected_value = _parse_value_condition_expression(value_condition)
        result = _compare_scalar_values(current, operator, expected_value)
    except (ValueError, TypeError) as exc:
        raise AssertionError(f"JSON 欄位 '{json_path}' 無法用值條件 '{value_condition}' 驗證") from exc
    return str(result)


@keyword("從回應取得JSON陣列長度")
def get_json_array_length_from_response(response: Any, json_path: str = "") -> str:
    try:
        current = _resolve_json_array(response.json(), json_path)
    except (KeyError, IndexError, TypeError, AssertionError) as exc:
        raise AssertionError(f"JSON 路徑 '{json_path}' 無法解析為陣列") from exc
    return str(len(current))


@keyword("從回應檢查JSON欄位集合")
def check_json_field_set_in_response(
    response: Any,
    expected_fields: str = "",
    match_mode: str = "contains",
    json_path: str = "",
) -> str:
    try:
        current = _resolve_json_path(response.json(), json_path, allow_blank=True)
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionError(f"JSON 路徑 '{json_path}' 無法解析欄位集合") from exc

    if not isinstance(current, dict):
        raise AssertionError(f"JSON 路徑 '{json_path}' 對應的值不是物件")

    expected_set = set(_parse_field_set(expected_fields))
    actual_set = set(current.keys())
    normalized_mode = (match_mode or "contains").strip().lower()
    if normalized_mode in ("contains", "includes"):
        return str(expected_set.issubset(actual_set))
    if normalized_mode == "exact":
        return str(expected_set == actual_set)
    raise AssertionError(f"不支援的欄位集合比對模式: {match_mode}")


@keyword("從回應檢查JSON欄位是否存在")
def json_field_exists_in_response(response: Any, json_path: str) -> str:
    exists, _ = _try_resolve_json_path(response.json(), json_path)
    return str(exists)


@keyword("從回應檢查JSON欄位是否為空")
def json_field_is_empty_in_response(response: Any, json_path: str) -> str:
    exists, value = _try_resolve_json_path(response.json(), json_path)
    if not exists:
        return "False"
    return str(_is_empty_value(value))


@keyword("從回應檢查JSON欄位不存在或為空")
def json_field_is_missing_or_empty_in_response(response: Any, json_path: str) -> str:
    exists, value = _try_resolve_json_path(response.json(), json_path)
    if not exists:
        return "True"
    return str(_is_empty_value(value))


@keyword("從回應依條件取得JSON陣列結果數量")
def get_json_array_match_count_by_condition_from_response(
    response: Any,
    condition_expression: str,
    array_path: str = "",
    query_options: str = "",
) -> str:
    try:
        matches = _find_matching_items(response.json(), condition_expression, array_path)
        matches = _apply_query_options(matches, query_options)
    except (KeyError, IndexError, TypeError, AssertionError, ValueError) as exc:
        raise AssertionError(f"條件式查找失敗: {exc}") from exc
    return str(len(matches))


@keyword("從回應依條件取得JSON陣列所有欄位值")
def get_json_array_all_field_values_by_condition_from_response(
    response: Any,
    condition_expression: str,
    target_path: str = "",
    array_path: str = "",
    query_options: str = "",
) -> str:
    try:
        matches = _find_matching_items(response.json(), condition_expression, array_path)
        matches = _apply_query_options(matches, query_options)
    except (KeyError, IndexError, TypeError, AssertionError, ValueError) as exc:
        raise AssertionError(f"條件式查找失敗: {exc}") from exc

    if not matches:
        return "[]"

    collected_values: List[Any] = []
    for item in matches:
        try:
            value = _resolve_json_path(item, target_path, allow_blank=True)
        except (KeyError, IndexError, TypeError) as exc:
            raise AssertionError(f"目標路徑 '{target_path}' 無法從符合條件的項目中取值") from exc
        collected_values.append(value)

    return json.dumps(collected_values, ensure_ascii=False)


@keyword("從回應依條件取得JSON陣列欄位值")
def get_json_array_field_value_by_condition_from_response(
    response: Any,
    condition_expression: str,
    target_path: str = "",
    array_path: str = "",
    query_options: str = "",
) -> str:
    try:
        matches = _find_matching_items(response.json(), condition_expression, array_path)
        matches = _apply_query_options(matches, query_options)
    except (KeyError, IndexError, TypeError, AssertionError, ValueError) as exc:
        raise AssertionError(f"條件式查找失敗: {exc}") from exc

    if not matches:
        raise AssertionError(f"找不到符合條件的 JSON 陣列項目: {condition_expression}")

    try:
        current = _resolve_json_path(matches, target_path, allow_blank=True)
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionError(f"目標路徑 '{target_path}' 無法從符合條件的項目中取值") from exc

    return _stringify_json_value(current)


@keyword("從回應依條件檢查JSON陣列項目是否存在")
def json_array_item_exists_by_condition_in_response(
    response: Any,
    condition_expression: str,
    array_path: str = "",
    query_options: str = "",
) -> str:
    try:
        matches = _find_matching_items(response.json(), condition_expression, array_path)
        matches = _apply_query_options(matches, query_options)
    except (KeyError, IndexError, TypeError, AssertionError, ValueError) as exc:
        raise AssertionError(f"條件式查找失敗: {exc}") from exc
    return str(bool(matches))


@keyword("從回應檢查JSON欄位符合正則")
def json_field_matches_regex_in_response(response: Any, json_path: str, pattern: str) -> str:
    try:
        current = _resolve_json_path(response.json(), json_path)
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionError(f"JSON 路徑 '{json_path}' 無法解析正則驗證") from exc
    return str(re.fullmatch(pattern, str(current)) is not None)


@keyword("從回應檢查JSON欄位日期格式")
def json_field_matches_date_format_in_response(response: Any, json_path: str, date_format: str) -> str:
    try:
        current = _resolve_json_path(response.json(), json_path)
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionError(f"JSON 路徑 '{json_path}' 無法解析日期格式驗證") from exc

    value = str(current)
    normalized_format = (date_format or "").strip()
    try:
        if normalized_format.lower() in ("iso8601", "iso-8601"):
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            datetime.strptime(value, normalized_format)
        return "True"
    except ValueError:
        return "False"


@keyword("從回應檢查JSON簡化Schema")
def check_json_simplified_schema_in_response(
    response: Any,
    schema_rules: str,
    json_path: str = "",
) -> str:
    try:
        current = _resolve_json_path(response.json(), json_path, allow_blank=True)
    except (KeyError, IndexError, TypeError) as exc:
        raise AssertionError(f"JSON 路徑 '{json_path}' 無法解析 Schema 驗證") from exc

    try:
        required_fields, type_rules = _parse_schema_rules(schema_rules)
    except ValueError as exc:
        raise AssertionError(str(exc)) from exc

    for required_field in required_fields:
        exists, _ = _try_resolve_json_path(current, required_field)
        if not exists:
            return "False"

    for field_path, expected_type in type_rules.items():
        if field_path == "":
            actual_value = current
        else:
            exists, actual_value = _try_resolve_json_path(current, field_path)
            if not exists:
                return "False"

        if _json_type_name(actual_value) != expected_type:
            return "False"

    return "True"