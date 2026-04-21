# Fastable Scan Page Test

Resource: ../../resources/core_keywords.resource
Test Case: 驗證 Fastable 掃描頁面主要欄位
Documentation: 登入後驗證掃描頁面的提示文字、查詢欄位、按鈕與狀態文案是否正常顯示。

| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |
| --- | --- | --- | --- | --- | --- | --- |
| Given | 開啟瀏覽器 | New Browser | chromium | headless=${TRUE} | | |
| And | 前往 Fastable 登入頁 | New Page | http://192.168.1.103/#/fastable/login | | | |
| And | 等待登入帳號欄位顯示 | Wait For Elements State | #username | visible ;; timeout=15s | | |
| When | 輸入 Fastable 帳號 | Browser.Fill Text | #username | %{FASTABLE_USERNAME} | | |
| And | 輸入 Fastable 密碼 | Browser.Fill Text | #password | %{FASTABLE_PASSWORD} | | |
| And | 點擊登入按鈕 | Browser.Click | text=登入 | | | |
| And | 等待登入後選單顯示 | Wait For Elements State | text=首頁 | visible ;; timeout=20s | | |
| When | 直接前往掃描頁面 | Go To | http://192.168.1.103/#/barcode | | | |
| Then | 等待掃描頁面提示顯示 | Wait For Elements State | text=輸入掃描檢測編號以新增箔卷 | visible ;; timeout=20s | | |
| And | 驗證掃描頁面提示內容 | Browser.Get Text | text=輸入掃描檢測編號以新增箔卷 | | == | 輸入掃描檢測編號以新增箔卷 |
| And | 驗證掃描頁面廠房欄位 | Browser.Get Text | text=廠房 | | == | 廠房 |
| And | 驗證掃描頁面產線欄位 | Browser.Get Text | text=產線 | | == | 產線 |
| And | 驗證掃描頁面設定版號欄位 | Browser.Get Text | text=設定版號 | | == | 設定版號 |
| And | 驗證掃描頁面卷號欄位 | Browser.Get Text | text=卷號 | | == | 卷號 |
| And | 驗證掃描頁面新增按鈕 | Browser.Get Text | text=新增 | | == | 新增 |
| And | 驗證掃描頁面開始檢測按鈕 | Browser.Get Text | xpath=(//button[normalize-space()='開始檢測'])[1] | | contains | 開始檢測 |
| And | 驗證掃描頁面警示狀態 | Browser.Get Text | text=警示狀態 ：等待檢測中... | | == | 警示狀態 ：等待檢測中... |
| And | 關閉瀏覽器 | Close Browser | | | | |