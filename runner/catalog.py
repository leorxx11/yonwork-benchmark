from __future__ import annotations

from dataclasses import dataclass

from .discovery import HostEndpoint
from .models import JsonObject
from .transport import request_json


# 模型候选列表。路由名字带 cron 是历史原因，返回的是全局候选，
# 和 /api/chat/apply-model 要的 {providerAccountId, modelId} 是同一套 id。
CHOICES_PATH = "/api/cron/visible-models"


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelChoice:
    provider_account_id: str
    model_id: str
    display_name: str = ""
    choice_id: str = ""
    is_default: bool = False

    @property
    def selection(self) -> JsonObject:
        """chat/send 的 modelSelection 字段。

        **字段名必须是 providerAccountId + modelId。** 写错名字不会报错，
        请求照样成功，只是被静默忽略、回落到默认模型——
        你以为在测 A，其实测的是 B，实测踩过。
        """
        return {"providerAccountId": self.provider_account_id, "modelId": self.model_id}

    @property
    def label(self) -> str:
        return f"{self.display_name or self.model_id}({self.provider_account_id}/{self.model_id})"


def list_model_choices(endpoint: HostEndpoint, timeout: float = 10.0) -> list[ModelChoice]:
    payload = request_json(endpoint.url(CHOICES_PATH), token=endpoint.token, timeout=timeout)
    raw = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise CatalogError(f"{CHOICES_PATH} 返回了预期外的结构：{str(payload)[:200]}")

    choices: list[ModelChoice] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        account = item.get("providerAccountId")
        model = item.get("modelId")
        if not isinstance(account, str) or not isinstance(model, str):
            continue
        choices.append(
            ModelChoice(
                provider_account_id=account,
                model_id=model,
                display_name=item.get("displayName") if isinstance(item.get("displayName"), str) else "",
                choice_id=item.get("choiceId") if isinstance(item.get("choiceId"), str) else "",
                is_default=bool(item.get("isDefaultModel")),
            )
        )
    if not choices:
        raise CatalogError(f"{CHOICES_PATH} 没有返回任何可用模型")
    return choices


def resolve_model_choice(choices: list[ModelChoice], query: str) -> ModelChoice:
    """按 choiceId / modelId / 显示名匹配，大小写不敏感。匹配不上就把候选列出来。"""

    wanted = query.strip().casefold()
    if not wanted:
        raise CatalogError("模型名不能为空")

    for attribute in ("choice_id", "model_id", "display_name"):
        hits = [item for item in choices if getattr(item, attribute).casefold() == wanted]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            listed = "、".join(item.label for item in hits)
            raise CatalogError(f"{query} 匹配到多个模型，请用 choiceId 指定：{listed}")

    listed = "\n  ".join(f"{item.label}{'（默认）' if item.is_default else ''}" for item in choices)
    raise CatalogError(f"找不到模型 {query}；当前可用：\n  {listed}")


def default_choice(choices: list[ModelChoice]) -> ModelChoice | None:
    return next((item for item in choices if item.is_default), None)
