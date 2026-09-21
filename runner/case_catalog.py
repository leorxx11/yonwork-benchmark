from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .models import CaseDefinition, Expectations


CATALOG_VERSION = 1
DEFAULT_CATALOG_PATH = Path("cases/catalog.yaml")
DEFAULT_CASE_SET = "smoke"

_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_.-]*$")
_ENV_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


class CaseCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CaseSetDefinition:
    case_set_id: str
    name: str
    description: str
    cases: tuple[CaseDefinition, ...]

    @property
    def enabled_count(self) -> int:
        return sum(1 for item in self.cases if item.enabled)

    @property
    def total_runs(self) -> int:
        return sum(item.runs for item in self.cases if item.enabled)

    @property
    def required_env(self) -> tuple[str, ...]:
        names = {
            match.group(1)
            for case in self.cases
            if case.enabled
            for match in _ENV_REFERENCE.finditer(case.prompt)
        }
        return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class CaseCatalog:
    path: Path
    case_sets: tuple[CaseSetDefinition, ...]

    def get(self, case_set_id: str) -> CaseSetDefinition:
        found = next(
            (item for item in self.case_sets if item.case_set_id == case_set_id), None
        )
        if found is None:
            choices = "、".join(item.case_set_id for item in self.case_sets)
            raise CaseCatalogError(
                f"Case Set 不存在：{case_set_id}；当前可用：{choices or '无'}"
            )
        return found


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CaseCatalogError(f"{where} 必须是对象")
    return value


def _only_keys(value: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise CaseCatalogError(f"{where} 含未知字段：{'、'.join(unknown)}")


def _text(value: Any, where: str, *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise CaseCatalogError(f"{where} 必须是非空字符串")
    return value.strip()


def _identifier(value: Any, where: str) -> str:
    identifier = _text(value, where)
    if not _ID.fullmatch(identifier):
        raise CaseCatalogError(
            f"{where} 只能包含字母、数字、点、下划线和连字符：{identifier!r}"
        )
    return identifier


def _positive_int(value: Any, where: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CaseCatalogError(f"{where} 必须是大于 0 的整数")
    return value


def _optional_number(value: Any, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise CaseCatalogError(f"{where} 必须是大于 0 的数字")
    return float(value)


def _optional_int(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CaseCatalogError(f"{where} 必须是大于 0 的整数")
    return value


def _keywords(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise CaseCatalogError(f"{where} 必须是字符串列表")
    result: list[str] = []
    for index, item in enumerate(value, start=1):
        result.append(_text(item, f"{where}[{index}]"))
    return tuple(result)


def _expectations(value: Any, where: str) -> Expectations:
    raw = {} if value is None else _mapping(value, where)
    _only_keys(
        raw,
        {
            "expect",
            "forbid",
            "min_length",
            "json_parsable",
            "max_seconds",
            "max_total_tokens",
            "max_input_tokens",
            "min_tool_calls",
        },
        where,
    )
    min_length = raw.get("min_length", 1)
    if isinstance(min_length, bool) or not isinstance(min_length, int) or min_length < 1:
        raise CaseCatalogError(f"{where}.min_length 必须是大于 0 的整数")
    json_parsable = raw.get("json_parsable", False)
    if not isinstance(json_parsable, bool):
        raise CaseCatalogError(f"{where}.json_parsable 必须是 true 或 false")
    return Expectations(
        expect_keywords=_keywords(raw.get("expect"), f"{where}.expect"),
        forbid_keywords=_keywords(raw.get("forbid"), f"{where}.forbid"),
        min_length=min_length,
        json_parsable=json_parsable,
        max_seconds=_optional_number(raw.get("max_seconds"), f"{where}.max_seconds"),
        max_total_tokens=_optional_int(
            raw.get("max_total_tokens"), f"{where}.max_total_tokens"
        ),
        max_input_tokens=_optional_int(
            raw.get("max_input_tokens"), f"{where}.max_input_tokens"
        ),
        min_tool_calls=_optional_int(
            raw.get("min_tool_calls"), f"{where}.min_tool_calls"
        ),
    )


def _case(value: Any, where: str) -> CaseDefinition:
    raw = _mapping(value, where)
    _only_keys(raw, {"id", "prompt", "runs", "enabled", "assertions"}, where)
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise CaseCatalogError(f"{where}.enabled 必须是 true 或 false")
    return CaseDefinition(
        case_name=_identifier(raw.get("id"), f"{where}.id"),
        prompt=_text(raw.get("prompt"), f"{where}.prompt"),
        runs=_positive_int(raw.get("runs"), f"{where}.runs", default=1),
        enabled=enabled,
        expectations=_expectations(raw.get("assertions"), f"{where}.assertions"),
    )


def load_catalog(path: Path = DEFAULT_CATALOG_PATH) -> CaseCatalog:
    if not path.is_file():
        raise CaseCatalogError(f"找不到 Case Catalog：{path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CaseCatalogError(f"Case Catalog 无法解析：{path}：{exc}") from exc

    root = _mapping(payload, str(path))
    _only_keys(root, {"version", "case_sets"}, str(path))
    if root.get("version") != CATALOG_VERSION:
        raise CaseCatalogError(
            f"{path} 的 version 必须是 {CATALOG_VERSION}，实际 {root.get('version')!r}"
        )
    raw_sets = root.get("case_sets")
    if not isinstance(raw_sets, list) or not raw_sets:
        raise CaseCatalogError(f"{path}.case_sets 必须是非空列表")

    case_sets: list[CaseSetDefinition] = []
    seen_sets: set[str] = set()
    for set_index, item in enumerate(raw_sets, start=1):
        where = f"{path}.case_sets[{set_index}]"
        raw = _mapping(item, where)
        _only_keys(raw, {"id", "name", "description", "cases"}, where)
        case_set_id = _identifier(raw.get("id"), f"{where}.id")
        if case_set_id in seen_sets:
            raise CaseCatalogError(f"{where}.id 重复：{case_set_id}")
        seen_sets.add(case_set_id)

        raw_cases = raw.get("cases")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise CaseCatalogError(f"{where}.cases 必须是非空列表")
        cases = tuple(
            _case(case, f"{where}.cases[{index}]")
            for index, case in enumerate(raw_cases, start=1)
        )
        names = [case.case_name for case in cases]
        duplicate = next((name for name in names if names.count(name) > 1), None)
        if duplicate:
            raise CaseCatalogError(f"{where} 的 Case id 重复：{duplicate}")

        description = raw.get("description", "")
        if not isinstance(description, str):
            raise CaseCatalogError(f"{where}.description 必须是字符串")
        case_sets.append(
            CaseSetDefinition(
                case_set_id=case_set_id,
                name=_text(raw.get("name"), f"{where}.name", default=case_set_id),
                description=description.strip(),
                cases=cases,
            )
        )
    return CaseCatalog(path=path, case_sets=tuple(case_sets))


def resolve_case_set(
    case_set: CaseSetDefinition,
    environment: Mapping[str, str] | None = None,
) -> CaseSetDefinition:
    """把 Prompt 里的 ${NAME} 替换成运行环境配置。

    路径等机器相关值不能写死在 Catalog 里；缺值时明确报错，不能把占位符发给模型。
    """

    values = environment if environment is not None else os.environ
    missing = [name for name in case_set.required_env if not values.get(name)]
    if missing:
        raise CaseCatalogError(
            f"Case Set {case_set.case_set_id} 缺少环境变量：{'、'.join(missing)}"
        )

    def substitute(prompt: str) -> str:
        return _ENV_REFERENCE.sub(
            lambda match: values.get(match.group(1), match.group(0)), prompt
        )

    return CaseSetDefinition(
        case_set_id=case_set.case_set_id,
        name=case_set.name,
        description=case_set.description,
        cases=tuple(
            CaseDefinition(
                case_name=case.case_name,
                prompt=substitute(case.prompt),
                runs=case.runs,
                enabled=case.enabled,
                expectations=case.expectations,
            )
            for case in case_set.cases
        ),
    )


def load_case_set(
    path: Path = DEFAULT_CATALOG_PATH,
    case_set_id: str = DEFAULT_CASE_SET,
    *,
    resolve_environment: bool = True,
) -> CaseSetDefinition:
    case_set = load_catalog(path).get(case_set_id)
    return resolve_case_set(case_set) if resolve_environment else case_set
