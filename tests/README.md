# 測試框架目錄結構

```
tests/
├── unit/                  # pytest 單元測試（後端 services/utils）
├── integration/           # pytest 整合測試（API + DB）
├── e2e/
│   ├── platform/
│   │   ├── api/           # 平台自身 REST API 的 Robot E2E
│   │   └── ui/            # 平台前端 UI 的 Robot E2E
│   └── samples/           # 業務範例：登入流程、PCHome 範例…
├── perf/locust/           # 效能測試（locust）
└── security/zap/          # 安全測試（OWASP ZAP）
tests_resources/           # Robot 共用 Resource / Library
tests_data/                # 測試資料（JSON / CSV）
results/                   # Robot 執行輸出（log.html, report.html, output.xml）
```

執行：
```powershell
python run_tests.py                      # 執行 tests/ 下所有 *.md
python run_tests.py -f tests/e2e/samples/integration_test.md
python run_tests.py -t "登入測試案例"
```
