-- 基准测试结果库。
--
-- 事实来源是 results/<batch>/results.jsonl，这里只是可重建的投影：
-- 任何时候都能 `python -m runner.ingest --rebuild` 从 JSONL 重放出来。
-- 所以这里**不存任何判定逻辑**，只存 assertions/ 已经算好的结论。
--
-- 时间列一律存 UTC，DATETIME(3) 保毫秒。

CREATE TABLE IF NOT EXISTS suite_runs (
    suite_id           VARCHAR(64)  NOT NULL PRIMARY KEY,
    name               VARCHAR(128) NOT NULL,
    prompt_sheet       VARCHAR(128) NOT NULL DEFAULT '',
    prompt_source_hash CHAR(64)     NOT NULL DEFAULT '',
    note               TEXT,
    created_at         DATETIME(3)  NOT NULL,
    UNIQUE KEY uk_suite_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS batches (
    batch_id    VARCHAR(64) NOT NULL PRIMARY KEY,
    suite_id    VARCHAR(64) NULL,
    -- product / model_mode 用 VARCHAR 不用 ENUM：模型会不断加，
    -- 'default' 是保留值，其余取模型的显示名（如 'newapi'）。
    product     VARCHAR(32) NOT NULL DEFAULT 'yonwork',
    model_mode  VARCHAR(64) NOT NULL DEFAULT 'default',
    model_ref   VARCHAR(128) NOT NULL DEFAULT '',
    agent_id    VARCHAR(64)  NOT NULL DEFAULT 'main',
    app_version VARCHAR(32)  NOT NULL DEFAULT '',
    started_at  DATETIME(3) NULL,
    ended_at    DATETIME(3) NULL,
    run_count   INT NOT NULL DEFAULT 0,
    KEY idx_batch_suite (suite_id),
    KEY idx_batch_mode (product, model_mode),
    CONSTRAINT fk_batch_suite FOREIGN KEY (suite_id)
        REFERENCES suite_runs (suite_id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS runs (
    benchmark_id      VARCHAR(128) NOT NULL PRIMARY KEY,
    batch_id          VARCHAR(64)  NOT NULL,
    position          INT          NOT NULL DEFAULT 0,
    case_name         VARCHAR(128) NOT NULL,
    run_no            INT          NOT NULL DEFAULT 1,
    prompt            MEDIUMTEXT   NOT NULL,
    session_key       VARCHAR(255) NOT NULL DEFAULT '',
    run_id            VARCHAR(128) NULL,
    -- 五类判定由 assertions/ 产出，ENUM 在库层面钉死分类，
    -- 免得哪天有人往里塞个 'Unknown' 把统计搅浑。
    verdict           ENUM('Pass','Fail','Timeout','Error','Invalid') NOT NULL,
    requested_model   VARCHAR(128) NULL,
    duration_ms       INT NULL,
    first_delta_ms    INT NULL,
    terminated_by     VARCHAR(64)  NULL,
    stop_reason       VARCHAR(64)  NULL,
    tool_call_count   INT NULL,
    answer_preview    MEDIUMTEXT   NULL,
    transcript_path   VARCHAR(512) NULL,
    note              TEXT,
    started_at        DATETIME(3)  NULL,
    ended_at          DATETIME(3)  NULL,
    created_at        DATETIME(3)  NOT NULL,
    KEY idx_run_batch (batch_id, position),
    KEY idx_run_case (case_name, run_no),
    KEY idx_run_verdict (verdict),
    CONSTRAINT fk_run_batch FOREIGN KEY (batch_id)
        REFERENCES batches (batch_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS checks (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    benchmark_id VARCHAR(128) NOT NULL,
    layer        ENUM('Completion','Artifact','Log','Content','Cost') NOT NULL,
    name         VARCHAR(64)  NOT NULL,
    -- NULL = 未执行（缺数据）。**别用 0 或 'Pass' 冒充**，
    -- 「没采到」和「没出错」是两回事。
    verdict      ENUM('Pass','Fail','Timeout','Error','Invalid') NULL,
    detail       TEXT,
    UNIQUE KEY uk_check (benchmark_id, layer, name),
    KEY idx_check_verdict (verdict),
    CONSTRAINT fk_check_run FOREIGN KEY (benchmark_id)
        REFERENCES runs (benchmark_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 同一轮允许多行，一个来源一行。端上漏记、后台有记录这类差异
-- 就是靠这张表存成数据，而不是在采集时丢掉。
CREATE TABLE IF NOT EXISTS usage_samples (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    benchmark_id      VARCHAR(128) NOT NULL,
    source            ENUM('device-api','session-jsonl','newapi') NOT NULL,
    model             VARCHAR(128) NULL,
    provider          VARCHAR(128) NULL,
    input_tokens      INT NULL,
    output_tokens     INT NULL,
    total_tokens      INT NULL,
    cache_read_tokens INT NULL,
    cache_write_tokens INT NULL,
    cost_usd          DECIMAL(12,6) NULL,
    api_calls         INT NULL,
    error_calls       INT NULL,
    -- 怎么对上的：run-id（精确）/ time-window（有张冠李戴风险）/ ...
    matched_by        VARCHAR(32) NOT NULL DEFAULT 'none',
    sampled_at        DATETIME(3) NULL,
    raw               JSON NULL,
    UNIQUE KEY uk_usage (benchmark_id, source),
    KEY idx_usage_model (model),
    CONSTRAINT fk_usage_run FOREIGN KEY (benchmark_id)
        REFERENCES runs (benchmark_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
