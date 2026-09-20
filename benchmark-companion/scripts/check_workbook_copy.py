from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

from benchmark_companion.excel_sync import ExcelResultsSync
from benchmark_companion.models import (
    RunRecord,
    RunStatus,
    SyncStatus,
    TokenStatus,
)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: check_workbook_copy.py WORKBOOK_COPY BACKUP_DIR")
    workbook = Path(sys.argv[1]).resolve()
    backup_dir = Path(sys.argv[2]).resolve()
    record_id = "integration-" + str(uuid.uuid4())
    timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    record = RunRecord(
        record_id=record_id,
        session_id=str(uuid.uuid4()),
        position=0,
        product="WorkBuddy",
        case_name="IntegrationCheck",
        run_no=1,
        prompt="Benchmark Companion integration check",
        start_time=timestamp,
        end_time=timestamp,
        duration_seconds=0.125,
        model="WorkBuddy默认模型",
        api_calls=None,
        error_calls=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        api_use_time=None,
        status=RunStatus.SUCCESS,
        token_status=TokenStatus.NOT_APPLICABLE,
        note="temporary workbook copy",
        sync_status=SyncStatus.PENDING,
        workbook_path=str(workbook),
        stats_script_path="",
        token_name="workbuddy",
        created_at=timestamp,
        updated_at=timestamp,
    )
    sync = ExcelResultsSync(workbook, backup_dir)
    first = sync.sync_records([record])
    second = sync.sync_records([record])
    print(
        json.dumps(
            {
                "record_id": record_id,
                "first_row": first.row_by_record[record_id],
                "second_row": second.row_by_record[record_id],
                "backup": str(first.backup_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

