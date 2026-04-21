-- ============================================================
-- AutoTest v1.0  Database Schema  (MySQL 8.0+)
-- 使用 utf8mb4 支援完整 Unicode（含 Emoji）
-- JSON 欄位需要 MySQL 5.7.8+；建議 8.0+
-- ============================================================

CREATE DATABASE IF NOT EXISTS autotest_db
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE autotest_db;

-- ────────────────────────────────────────────────────────────
-- 1. 專案表 projects
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projects (
    id         VARCHAR(36)  NOT NULL,
    name       VARCHAR(200) NOT NULL,
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX idx_projects_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ────────────────────────────────────────────────────────────
-- 2. 目錄樹節點表 tree_nodes  (Adjacency List 模型)
-- level_type 嚴格遵守 5 層級：
--   (根) → FEATURE → PLATFORM → PAGE → SCENARIO → TESTCASE
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tree_nodes (
    id          VARCHAR(36)   NOT NULL,
    project_id  VARCHAR(36)   NOT NULL,
    parent_id   VARCHAR(36)       NULL,
    level_type  ENUM(
                    'FEATURE',
                    'PLATFORM',
                    'PAGE',
                    'SCENARIO',
                    'TESTCASE'
                )             NOT NULL,
    name        VARCHAR(300)  NOT NULL,
    sort_order  INT           NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    CONSTRAINT fk_tree_project
        FOREIGN KEY (project_id) REFERENCES projects(id)   ON DELETE CASCADE,
    CONSTRAINT fk_tree_parent
        FOREIGN KEY (parent_id)  REFERENCES tree_nodes(id) ON DELETE CASCADE,
    INDEX idx_tree_project (project_id),
    INDEX idx_tree_parent  (parent_id),
    INDEX idx_tree_sort    (project_id, sort_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ────────────────────────────────────────────────────────────
-- 3. 測試案例內容表 testcase_contents
--    1 對 1 對應 tree_nodes（level_type = 'TESTCASE'）
--    steps_json 格式：[{"id":"...","keyword":"Given","action":"...",...}]
--    ddt_json   格式：{"headers":["$Acct","$Pwd"],"rows":[["admin","1234"]]}
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS testcase_contents (
    node_id    VARCHAR(36) NOT NULL,
    ac_text    TEXT            NULL COMMENT '驗收準則 (ATDD)',
    setup_text TEXT            NULL COMMENT '前置動作 (Pre-Setup)',
    steps_json JSON            NULL COMMENT 'BDD 步驟陣列',
    ddt_json   JSON            NULL COMMENT 'Data-Driven 表格',
    PRIMARY KEY (node_id),
    CONSTRAINT fk_tc_node
        FOREIGN KEY (node_id) REFERENCES tree_nodes(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ────────────────────────────────────────────────────────────
-- 4. 執行報告總表 execution_reports
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS execution_reports (
    id           VARCHAR(36) NOT NULL,
    task_id      VARCHAR(36)     NULL COMMENT 'Celery task_id，用於 GET /executions/{task_id}/status 查詢',
    project_id   VARCHAR(36) NOT NULL,
    trigger_type VARCHAR(50) NOT NULL DEFAULT 'Manual'
                             COMMENT 'Manual | CI/CD Scheduled',
    status       ENUM('RUNNING','PASSED','FAILED')
                             NOT NULL DEFAULT 'RUNNING',
    duration_ms  INT         NOT NULL DEFAULT 0,
    total_cases  INT         NOT NULL DEFAULT 0,
    passed_cases INT         NOT NULL DEFAULT 0,
    failed_cases INT         NOT NULL DEFAULT 0,
    created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    CONSTRAINT fk_report_project
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    INDEX idx_report_project (project_id),
    INDEX idx_report_task_id (task_id),
    INDEX idx_report_status  (status),
    INDEX idx_report_created (created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ── 如果 execution_reports 已存在（升級情境），補充 task_id 欄位 ──
-- （新資料庫可直接忽略此段）
ALTER TABLE execution_reports
    ADD COLUMN IF NOT EXISTS task_id VARCHAR(36) NULL
        COMMENT 'Celery task_id，用於 GET /api/v1/executions/{task_id}/status 查詢'
    AFTER id;

ALTER TABLE execution_reports
    ADD INDEX IF NOT EXISTS idx_report_task_id (task_id);


-- ────────────────────────────────────────────────────────────
-- 5. 執行步驟詳細表 execution_steps_log
--    testcase_node_id 刪除時設為 NULL（保留歷史截圖紀錄）
-- ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS execution_steps_log (
    id                   VARCHAR(36) NOT NULL,
    report_id            VARCHAR(36) NOT NULL,
    testcase_node_id     VARCHAR(36)     NULL,
    step_index           INT         NOT NULL DEFAULT 0,
    status               ENUM('RUNNING','PASSED','FAILED')
                                     NOT NULL DEFAULT 'RUNNING',
    duration_ms          INT         NOT NULL DEFAULT 0,
    error_message        TEXT            NULL,

    -- UI 截圖欄位（圖片存 PIC 資料夾，此欄只存路徑字串）
    pre_screenshot_url   VARCHAR(500)    NULL COMMENT '執行前截圖 URL',
    post_screenshot_url  VARCHAR(500)    NULL COMMENT '執行後截圖 URL',
    target_highlight_json JSON           NULL
        COMMENT '紅框座標 {"top":"35%","left":"25%","width":"50%","height":"10%"}',

    -- API 測試欄位
    req_payload_json     JSON            NULL COMMENT 'HTTP 請求內容',
    res_payload_json     JSON            NULL COMMENT 'HTTP 回應內容',

    PRIMARY KEY (id),
    CONSTRAINT fk_step_report
        FOREIGN KEY (report_id)        REFERENCES execution_reports(id) ON DELETE CASCADE,
    CONSTRAINT fk_step_node
        FOREIGN KEY (testcase_node_id) REFERENCES tree_nodes(id)        ON DELETE SET NULL,
    INDEX idx_step_report (report_id),
    INDEX idx_step_node   (testcase_node_id),
    INDEX idx_step_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ────────────────────────────────────────────────────────────
-- 驗證用種子資料（可選）
-- ────────────────────────────────────────────────────────────
INSERT IGNORE INTO projects (id, name) VALUES
    ('proj-aoi-001', 'AOI 測試專案'),
    ('proj-web-002', 'Web 前端測試專案');

INSERT IGNORE INTO tree_nodes (id, project_id, parent_id, level_type, name, sort_order) VALUES
    ('node-f-001', 'proj-aoi-001', NULL,        'FEATURE',  '登入功能',       1),
    ('node-p-001', 'proj-aoi-001', 'node-f-001','PLATFORM', 'Web Chrome',     1),
    ('node-g-001', 'proj-aoi-001', 'node-p-001','PAGE',     '登入頁面',       1),
    ('node-s-001', 'proj-aoi-001', 'node-g-001','SCENARIO', '正常登入情境',   1),
    ('node-t-001', 'proj-aoi-001', 'node-s-001','TESTCASE', 'TC-001 帳號密碼登入', 1);

INSERT IGNORE INTO testcase_contents (node_id, ac_text, steps_json, ddt_json) VALUES (
    'node-t-001',
    '給定一個已啟用帳號，當輸入正確帳號密碼，則應成功進入系統首頁',
    JSON_ARRAY(
        JSON_OBJECT('id','s1','keyword','Given','action','開啟登入頁面','target','https://example.com/login','value','','expected','','status','','notes',''),
        JSON_OBJECT('id','s2','keyword','When', 'action','輸入帳號',     'target','#username',               'value','$Acct','expected','','status','','notes',''),
        JSON_OBJECT('id','s3','keyword','And',  'action','輸入密碼',     'target','#password',               'value','$Pwd', 'expected','','status','','notes',''),
        JSON_OBJECT('id','s4','keyword','Then', 'action','應看到首頁標題','target','h1.title',               'value','','expected','歡迎回來','status','','notes','')
    ),
    JSON_OBJECT(
        'headers', JSON_ARRAY('$Acct','$Pwd'),
        'rows',    JSON_ARRAY(JSON_ARRAY('admin','admin123'), JSON_ARRAY('tester','test456'))
    )
);
