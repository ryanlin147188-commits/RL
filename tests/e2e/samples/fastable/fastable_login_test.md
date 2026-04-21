# Fastable Login Test

Resource: ../../resources/core_keywords.resource
Test Case: 驗證 Fastable 登入頁與登入後首頁
Documentation: 驗證登入頁主要文案與欄位，並使用環境變數帳密登入後切換到首頁確認主要品牌標語。

| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |
| --- | --- | --- | --- | --- | --- | --- |
| Given | 開啟瀏覽器 | New Browser | chromium | headless=${TRUE} | | |
| And | 前往 Fastable 登入頁 | New Page | http://192.168.1.103/#/fastable/login | | | |
| And | 等待登入帳號欄位顯示 | Wait For Elements State | #username | visible ;; timeout=15s | | |
| Then | 驗證登入頁品牌名稱 | Browser.Get Text | text=fastable.ai | | == | fastable.ai |
| And | 驗證登入頁品牌標語 | Browser.Get Text | text=Fastable = Fast + Stable | | == | Fastable = Fast + Stable |
| And | 驗證登入頁主標題 | Browser.Get Text | text=高速且穩定的 AI 檢測解決方案 | | == | 高速且穩定的 AI 檢測解決方案 |
| And | 驗證電子郵件欄位標題 | Browser.Get Text | text=電子郵件 | | == | 電子郵件 |
| And | 驗證密碼欄位標題 | Browser.Get Text | text=密碼 | | == | 密碼 |
| When | 輸入 Fastable 帳號 | Browser.Fill Text | #username | %{FASTABLE_USERNAME} | | |
| And | 輸入 Fastable 密碼 | Browser.Fill Text | #password | %{FASTABLE_PASSWORD} | | |
| And | 點擊登入按鈕 | Browser.Click | text=登入 | | | |
| Then | 等待登入後選單顯示 | Wait For Elements State | text=首頁 | visible ;; timeout=20s | | |
| When | 切換到首頁 | Browser.Click | text=首頁 | | | |
| And | 等待首頁品牌標語顯示 | Wait For Elements State | text=Fastable = Fast + Stable | visible ;; timeout=15s | | |
| Then | 驗證首頁品牌標語 | Browser.Get Text | text=Fastable = Fast + Stable | | == | Fastable = Fast + Stable |
| And | 驗證首頁主標題 | Browser.Get Text | text=高速且穩定的 AI 檢測解決方案 | | == | 高速且穩定的 AI 檢測解決方案 |
| And | 關閉瀏覽器 | Close Browser | | | | |