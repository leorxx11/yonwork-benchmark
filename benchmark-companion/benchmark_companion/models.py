from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ModelMode(str, Enum):
    NEW_API = "NewAPI"
    WORKBUDDY_DEFAULT = "WorkBuddyDefault"

    @property
    def model_name(self) -> str:
        if self is ModelMode.NEW_API:
            return "deepseek-flash"
        return "WorkBuddy默认模型"


class RunStatus(str, Enum):
    SUCCESS = "Success"
    FAILED = "Failed"
    SKIPPED = "Skipped"


class TokenStatus(str, Enum):
    PENDING = "Pending"
    SUCCESS = "Success"
    NO_DATA = "NoData"
    ERROR = "Error"
    NOT_APPLICABLE = "NotApplicable"


class SyncStatus(str, Enum):
    PENDING = "Pending"
    SYNCED = "Synced"


@dataclass(frozen=True, slots=True)
class CaseDefinition:
    case_name: str
    prompt: str
    runs: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class TaskItem:
    position: int
    case_name: str
    run_no: int
    prompt: str
    state: str = "Pending"
    start_time: str | None = None
    record_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionInfo:
    session_id: str
    model_mode: ModelMode
    model_name: str
    workbook_path: str
    prompt_sheet: str
    stats_script_path: str
    token_name: str
    total_items: int
    current_position: int
    status: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TokenStats:
    api_calls: int
    error_calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    api_use_time: float


@dataclass(frozen=True, slots=True)
class RunRecord:
    record_id: str
    session_id: str
    position: int
    product: str
    case_name: str
    run_no: int
    prompt: str
    start_time: str | None
    end_time: str | None
    duration_seconds: float | None
    model: str
    api_calls: int | None
    error_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    api_use_time: float | None
    status: RunStatus
    token_status: TokenStatus
    note: str
    sync_status: SyncStatus
    workbook_path: str
    stats_script_path: str
    token_name: str
    created_at: str
    updated_at: str
    excel_row: int | None = None


def expand_cases(cases: list[CaseDefinition]) -> list[TaskItem]:
    items: list[TaskItem] = []
    for case in cases:
        if not case.enabled:
            continue
        for run_no in range(1, case.runs + 1):
            items.append(
                TaskItem(
                    position=len(items),
                    case_name=case.case_name,
                    run_no=run_no,
                    prompt=case.prompt,
                )
            )
    return items
