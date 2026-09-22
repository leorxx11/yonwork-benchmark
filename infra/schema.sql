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
    -- 产品自报的内部耗时，不含每轮起进程的冷启动；冷启动 = duration_ms - engine_ms。
    -- 只有 WorkBuddy 这类「每轮一个进程」的产品有值，YonWork 常驻服务留 NULL。
    -- ⚠️ 与 runner/ingest.py 的按需 ALTER 是两份，加列时两处都要改。
    engine_ms         INT NULL,
    -- 逐请求采集这一轮的状态：disabled（没开）/ observed（采到了）/
    -- unavailable（开着却一个请求都没经过入口）。
    -- **「没开」和「0 次调用」必须分开**，报 0 就是六-3 那种静默漏记。
    -- ⚠️ 与 runner/ingest.py 的按需 ALTER 是两份，加列时两处都要改。
    model_calls_status VARCHAR(16) NOT NULL DEFAULT 'disabled',
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
    -- 取值见 runner/models.py 的 USAGE_SOURCES。
    -- **故意不用 ENUM**：每接一个新产品就会多一个来源（workbuddy-cli 就是这么来的），
    -- ENUM 会让每次都要改表——而这张表只在数据目录为空时建一次，
    -- 已有库根本跑不到这里，新来源会在入库时被静默拒掉。
    source            VARCHAR(32) NOT NULL,
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

-- 逐请求模型调用账本（runner/modelproxy）。一次**客户端到代理**的请求一行。
-- 事实来源是 results/<batch>/model-requests.jsonl，这张表随时可重建。
--
-- benchmark_id 允许 NULL：归属不上的请求（没带产品原生关联头）依然要留下来，
-- 它属于这个批次但不属于任何一轮。**绝不按时间窗硬塞给某一轮。**
-- 所以外键挂在 batch_id 上，不是 benchmark_id。
CREATE TABLE IF NOT EXISTS model_requests (
    -- 代理分配的稳定 ID。重放/重复导入靠它幂等，别换成自增主键。
    request_id         VARCHAR(160) NOT NULL PRIMARY KEY,
    batch_id           VARCHAR(64)  NOT NULL,
    benchmark_id       VARCHAR(128) NULL,
    proxy_id           VARCHAR(128) NOT NULL DEFAULT '',
    sequence           INT NOT NULL DEFAULT 0,
    -- attributed / late / unattributed / rejected。late 仍归旧轮，不并入下一轮。
    attribution        VARCHAR(16)  NOT NULL DEFAULT 'unattributed',
    attribution_source VARCHAR(64)  NOT NULL DEFAULT '',
    product            VARCHAR(32)  NULL,
    protocol           VARCHAR(32)  NOT NULL DEFAULT '',
    requested_model    VARCHAR(128) NULL,
    -- 响应自称的模型，**不等于已证明的真实部署模型**。
    response_model     VARCHAR(128) NULL,
    is_stream          TINYINT(1)   NULL,
    http_status        INT NULL,
    -- completed / stream-truncated / upstream-error / client-disconnected /
    -- transport-error / timeout / refused / incomplete
    termination        VARCHAR(32)  NULL,
    -- 首个**有效**输出：带内容或工具参数的那一片，响应头和空事件不算。
    first_output_ms    INT NULL,
    duration_ms        INT NULL,
    input_tokens       INT NULL,
    output_tokens      INT NULL,
    total_tokens       INT NULL,
    cache_read_tokens  INT NULL,
    -- observed / missing。**缺就是缺，不补零。**
    usage_status       VARCHAR(16)  NOT NULL DEFAULT 'missing',
    output_events      INT NULL,
    -- W3C 跟踪头的值。YonWork 1.0.10 起是唯一可能的关联线索，
    -- 补关联的前提是请求发生时就存下来。见 docs/yonwork-1.0.10-correlation-probe.md。
    traceparent        VARCHAR(128) NULL,
    upstream_request_id VARCHAR(128) NULL,
    -- **恒为 NULL**：一次客户端请求不等于一次上游尝试，网关内部重试看不见。
    -- 填 1 就是拿观测不到的东西冒充证据。
    upstream_attempts  INT NULL,
    error_kind         VARCHAR(64)  NOT NULL DEFAULT '',
    received_at        DATETIME(3)  NULL,
    ended_at           DATETIME(3)  NULL,
    raw                JSON NULL,
    KEY idx_mreq_run (benchmark_id, sequence),
    KEY idx_mreq_batch (batch_id, sequence),
    KEY idx_mreq_attribution (attribution),
    KEY idx_mreq_upstream (upstream_request_id),
    KEY idx_mreq_trace (traceparent),
    CONSTRAINT fk_mreq_batch FOREIGN KEY (batch_id)
        REFERENCES batches (batch_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Web 控制台提交的执行任务。任务状态属于控制面，不是模型判定结果。
-- ⚠️ 与 runner/job_store.py 的 JOBS_SCHEMA 是有意重复的两份：这个文件只在
-- 数据目录为空时由 MySQL 镜像执行一次，改一处必须同步改另一处。
-- plan_* 三列的按需迁移在 job_store._ensure_plan_columns，旧库靠那里补。
CREATE TABLE IF NOT EXISTS benchmark_jobs (
    job_id             CHAR(32)     NOT NULL PRIMARY KEY,
    -- 一次「跨模式一键提交」的分组。NULL = 单模式任务。
    plan_id            CHAR(32)     NULL,
    plan_position      INT          NOT NULL DEFAULT 0,
    plan_label         VARCHAR(128) NOT NULL DEFAULT '',
    batch_id           VARCHAR(64)  NOT NULL,
    experiment_name    VARCHAR(128) NOT NULL,
    case_catalog_path  VARCHAR(512) NOT NULL DEFAULT 'cases/catalog.yaml',
    case_set_id        VARCHAR(64)  NOT NULL,
    product            VARCHAR(32)  NOT NULL DEFAULT 'yonwork',
    model_query        VARCHAR(128) NOT NULL DEFAULT '',
    agent_id           VARCHAR(64)  NOT NULL DEFAULT 'main',
    timeout_seconds    DECIMAL(10,3) NOT NULL DEFAULT 600,
    limit_runs         INT NOT NULL DEFAULT 0,
    collect_usage      BOOLEAN NOT NULL DEFAULT TRUE,
    export_xlsx        BOOLEAN NOT NULL DEFAULT TRUE,
    -- 只有 WorkBuddy 消费它；YonWork 的工具由智能体配置决定，关不掉。
    allow_tools        BOOLEAN NOT NULL DEFAULT FALSE,
    status             ENUM('Queued','Running','Completed','Failed','Cancelled')
                       NOT NULL DEFAULT 'Queued',
    total_runs         INT NOT NULL DEFAULT 0,
    completed_runs     INT NOT NULL DEFAULT 0,
    suite_id           VARCHAR(64) NULL,
    results_path       VARCHAR(512) NULL,
    worker_id          VARCHAR(128) NULL,
    cancel_requested   BOOLEAN NOT NULL DEFAULT FALSE,
    error              TEXT,
    created_at         DATETIME(3) NOT NULL,
    started_at         DATETIME(3) NULL,
    finished_at        DATETIME(3) NULL,
    updated_at         DATETIME(3) NOT NULL,
    UNIQUE KEY uk_job_batch (batch_id),
    KEY idx_job_status_created (status, created_at),
    KEY idx_job_plan (plan_id, plan_position)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS benchmark_job_events (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    job_id      CHAR(32) NOT NULL,
    level       VARCHAR(16) NOT NULL DEFAULT 'info',
    message     TEXT NOT NULL,
    created_at  DATETIME(3) NOT NULL,
    KEY idx_job_event (job_id, id),
    CONSTRAINT fk_job_event FOREIGN KEY (job_id)
        REFERENCES benchmark_jobs (job_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
