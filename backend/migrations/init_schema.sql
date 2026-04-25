-- ============================================================
-- AutoTest v1.0  Database Schema  (PostgreSQL 14+)
-- 全 UTF-8（PostgreSQL 預設 UNICODE）；JSONB 為原生型別。
-- ============================================================

-- ── ENUM 型別（必須先建立才能在欄位上使用）──────────────
DO $$ BEGIN
    CREATE TYPE tree_level_type AS ENUM (
        'FEATURE', 'PLATFORM', 'PAGE', 'SCENARIO', 'TESTCASE'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE execution_status AS ENUM ('RUNNING', 'PASSED', 'FAILED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE device_platform AS ENUM ('ANDROID', 'IOS');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;


-- ── 共用：updated_at 自動更新 trigger 函式 ──────────────
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ────────────────────────────────────────────────────────────
-- 1. 專案表 projects
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projects (
    id         VARCHAR(36)  NOT NULL,
    name       VARCHAR(200) NOT NULL,
    created_at TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_projects_name ON projects (name);

DROP TRIGGER IF EXISTS trg_projects_updated_at ON projects;
CREATE TRIGGER trg_projects_updated_at BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ────────────────────────────────────────────────────────────
-- 2. 目錄樹節點表 tree_nodes  (Adjacency List 模型)
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tree_nodes (
    id          VARCHAR(36)        NOT NULL,
    project_id  VARCHAR(36)        NOT NULL,
    parent_id   VARCHAR(36),
    level_type  tree_level_type    NOT NULL,
    name        VARCHAR(300)       NOT NULL,
    sort_order  INTEGER            NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    CONSTRAINT fk_tree_project
        FOREIGN KEY (project_id) REFERENCES projects(id)   ON DELETE CASCADE,
    CONSTRAINT fk_tree_parent
        FOREIGN KEY (parent_id)  REFERENCES tree_nodes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tree_project ON tree_nodes (project_id);
CREATE INDEX IF NOT EXISTS idx_tree_parent  ON tree_nodes (parent_id);
CREATE INDEX IF NOT EXISTS idx_tree_sort    ON tree_nodes (project_id, sort_order);


-- ────────────────────────────────────────────────────────────
-- 3. 測試案例內容表 testcase_contents
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS testcase_contents (
    node_id    VARCHAR(36) NOT NULL,
    ac_text    TEXT,
    setup_text TEXT,
    steps_json JSONB,
    ddt_json   JSONB,
    PRIMARY KEY (node_id),
    CONSTRAINT fk_tc_node
        FOREIGN KEY (node_id) REFERENCES tree_nodes(id) ON DELETE CASCADE
);
COMMENT ON COLUMN testcase_contents.ac_text    IS '驗收準則 (ATDD)';
COMMENT ON COLUMN testcase_contents.setup_text IS '前置動作 (Pre-Setup)';
COMMENT ON COLUMN testcase_contents.steps_json IS 'BDD 步驟陣列';
COMMENT ON COLUMN testcase_contents.ddt_json   IS 'Data-Driven 表格';


-- ────────────────────────────────────────────────────────────
-- 4. 執行報告總表 execution_reports
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS execution_reports (
    id               VARCHAR(36)      NOT NULL,
    task_id          VARCHAR(36),
    project_id       VARCHAR(36)      NOT NULL,
    trigger_type     VARCHAR(50)      NOT NULL DEFAULT 'Manual',
    status           execution_status NOT NULL DEFAULT 'RUNNING',
    duration_ms      INTEGER          NOT NULL DEFAULT 0,
    total_cases      INTEGER          NOT NULL DEFAULT 0,
    passed_cases     INTEGER          NOT NULL DEFAULT 0,
    failed_cases     INTEGER          NOT NULL DEFAULT 0,
    enable_recording SMALLINT         NOT NULL DEFAULT 1,
    created_at       TIMESTAMP        NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id),
    CONSTRAINT fk_report_project
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_report_project ON execution_reports (project_id);
CREATE INDEX IF NOT EXISTS idx_report_task_id ON execution_reports (task_id);
CREATE INDEX IF NOT EXISTS idx_report_status  ON execution_reports (status);
CREATE INDEX IF NOT EXISTS idx_report_created ON execution_reports (created_at DESC);

COMMENT ON COLUMN execution_reports.task_id          IS 'Celery task_id';
COMMENT ON COLUMN execution_reports.trigger_type     IS 'Manual | Scheduled | <username>';
COMMENT ON COLUMN execution_reports.enable_recording IS '0=關閉、1=啟用 Trace+Video';


-- ────────────────────────────────────────────────────────────
-- 5. 執行步驟詳細表 execution_steps_log
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS execution_steps_log (
    id                      VARCHAR(36)      NOT NULL,
    report_id               VARCHAR(36)      NOT NULL,
    testcase_node_id        VARCHAR(36),
    step_index              INTEGER          NOT NULL DEFAULT 0,
    status                  execution_status NOT NULL DEFAULT 'RUNNING',
    duration_ms             INTEGER          NOT NULL DEFAULT 0,
    error_message           TEXT,

    -- UI 截圖欄位
    pre_screenshot_url      VARCHAR(500),
    post_screenshot_url     VARCHAR(500),
    target_highlight_json   JSONB,

    -- API 測試欄位
    req_payload_json        JSONB,
    res_payload_json        JSONB,

    -- Trace（軌跡追蹤）/ Video（錄影）欄位
    trace_url               VARCHAR(500),
    video_url               VARCHAR(500),
    step_video_url          VARCHAR(500),

    -- Screenshot diff 欄位
    screenshot_baseline_url VARCHAR(500),
    screenshot_diff_url     VARCHAR(500),
    screenshot_diff_pct     DOUBLE PRECISION,

    PRIMARY KEY (id),
    CONSTRAINT fk_step_report
        FOREIGN KEY (report_id)        REFERENCES execution_reports(id) ON DELETE CASCADE,
    CONSTRAINT fk_step_node
        FOREIGN KEY (testcase_node_id) REFERENCES tree_nodes(id)        ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_step_report ON execution_steps_log (report_id);
CREATE INDEX IF NOT EXISTS idx_step_node   ON execution_steps_log (testcase_node_id);
CREATE INDEX IF NOT EXISTS idx_step_status ON execution_steps_log (status);


-- ────────────────────────────────────────────────────────────
-- 6. Screenshot baselines per step
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS step_screenshot_baselines (
    step_uuid         VARCHAR(36)      NOT NULL,
    testcase_node_id  VARCHAR(36),
    baseline_url      TEXT             NOT NULL,
    threshold_pct     DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    created_at        TIMESTAMP        NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMP        NOT NULL DEFAULT NOW(),
    PRIMARY KEY (step_uuid),
    CONSTRAINT fk_baseline_node
        FOREIGN KEY (testcase_node_id) REFERENCES tree_nodes(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_baseline_node ON step_screenshot_baselines (testcase_node_id);

DROP TRIGGER IF EXISTS trg_baseline_updated_at ON step_screenshot_baselines;
CREATE TRIGGER trg_baseline_updated_at BEFORE UPDATE ON step_screenshot_baselines
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ────────────────────────────────────────────────────────────
-- 7. 全專案共用設定：環境變數 / 設備資訊
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project_env_vars (
    id           VARCHAR(36)  NOT NULL,
    project_id   VARCHAR(36)  NOT NULL,
    name         VARCHAR(100) NOT NULL,
    value        TEXT         NOT NULL,
    description  VARCHAR(500),
    created_at   TIMESTAMP    NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMP    NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id),
    CONSTRAINT fk_envvar_project
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    CONSTRAINT uq_envvar_project_name UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_envvar_project ON project_env_vars (project_id);

DROP TRIGGER IF EXISTS trg_envvar_updated_at ON project_env_vars;
CREATE TRIGGER trg_envvar_updated_at BEFORE UPDATE ON project_env_vars
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


CREATE TABLE IF NOT EXISTS project_devices (
    id                VARCHAR(36)     NOT NULL,
    project_id        VARCHAR(36)     NOT NULL,
    label             VARCHAR(100)    NOT NULL,
    platform          device_platform NOT NULL,
    platform_version  VARCHAR(20),
    device_name       VARCHAR(100),
    avd_name          VARCHAR(100),
    udid              VARCHAR(100),
    automation_name   VARCHAR(50),
    extra_caps_json   JSONB,
    created_at        TIMESTAMP       NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMP       NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id),
    CONSTRAINT fk_device_project
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    CONSTRAINT uq_device_project_label UNIQUE (project_id, label)
);
CREATE INDEX IF NOT EXISTS idx_device_project ON project_devices (project_id);

DROP TRIGGER IF EXISTS trg_device_updated_at ON project_devices;
CREATE TRIGGER trg_device_updated_at BEFORE UPDATE ON project_devices
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ────────────────────────────────────────────────────────────
-- 8. 錄製功能 recording_sessions
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS recording_sessions (
    id          VARCHAR(36) NOT NULL,
    project_id  VARCHAR(36),
    target_url  TEXT        NOT NULL,
    status      VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    script_text TEXT,
    trace_path  VARCHAR(500),
    created_at  TIMESTAMP   NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMP   NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_rec_project ON recording_sessions (project_id);
CREATE INDEX IF NOT EXISTS idx_rec_status  ON recording_sessions (status);

DROP TRIGGER IF EXISTS trg_rec_updated_at ON recording_sessions;
CREATE TRIGGER trg_rec_updated_at BEFORE UPDATE ON recording_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ────────────────────────────────────────────────────────────
-- 驗證用種子資料（可選；ON CONFLICT DO NOTHING 避免重複）
-- ────────────────────────────────────────────────────────────
INSERT INTO projects (id, name) VALUES
    ('proj-aoi-001', 'AOI 測試專案'),
    ('proj-web-002', 'Web 前端測試專案')
ON CONFLICT (id) DO NOTHING;

INSERT INTO tree_nodes (id, project_id, parent_id, level_type, name, sort_order) VALUES
    ('node-f-001', 'proj-aoi-001', NULL,        'FEATURE',  '登入功能',       1),
    ('node-p-001', 'proj-aoi-001', 'node-f-001','PLATFORM', 'Web Chrome',     1),
    ('node-g-001', 'proj-aoi-001', 'node-p-001','PAGE',     '登入頁面',       1),
    ('node-s-001', 'proj-aoi-001', 'node-g-001','SCENARIO', '正常登入情境',   1),
    ('node-t-001', 'proj-aoi-001', 'node-s-001','TESTCASE', 'TC-001 帳號密碼登入', 1)
ON CONFLICT (id) DO NOTHING;

INSERT INTO testcase_contents (node_id, ac_text, steps_json, ddt_json) VALUES (
    'node-t-001',
    '給定一個已啟用帳號，當輸入正確帳號密碼，則應成功進入系統首頁',
    '[
        {"id":"s1","keyword":"Given","action":"開啟登入頁面","target":"https://example.com/login","value":"","expected":"","status":"","notes":""},
        {"id":"s2","keyword":"When","action":"輸入帳號","target":"#username","value":"$Acct","expected":"","status":"","notes":""},
        {"id":"s3","keyword":"And","action":"輸入密碼","target":"#password","value":"$Pwd","expected":"","status":"","notes":""},
        {"id":"s4","keyword":"Then","action":"應看到首頁標題","target":"h1.title","value":"","expected":"歡迎回來","status":"","notes":""}
    ]'::jsonb,
    '{"headers":["$Acct","$Pwd"],"rows":[["admin","admin123"],["tester","test456"]]}'::jsonb
)
ON CONFLICT (node_id) DO NOTHING;
