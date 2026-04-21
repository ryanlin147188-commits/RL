# Fastable Page Validation Template

Resource: ../../resources/core_keywords.resource
Test Case: 驗證 Fastable 登入後各功能頁主要 DOM 元素
Documentation: 依 Fastable 目前登入頁、首頁、掃描頁面、即時監控、調閱頁面、邊緣設定與設定頁面的實際 DOM 與可見文字整理而成。案例以穩定文字與主要區塊為核心，避免使用錄製工具產生的脆弱 selector。

| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |
| --- | --- | --- | --- | --- | --- | --- |
| Given | 開啟瀏覽器 | New Browser | chromium | headless=${FALSE} | | |
| And | 前往 Fastable 登入頁 | New Page | http://192.168.1.103/#/fastable/login | | | |
| And | 等待登入帳號欄位顯示 | Wait For Elements State | #username | visible ;; timeout=15s | | |
| Then | 驗證登入頁品牌名稱 | Browser.Get Text | text=fastable.ai | | == | fastable.ai |
| And | 驗證登入頁品牌標語 | Browser.Get Text | text=Fastable = Fast + Stable | | == | Fastable = Fast + Stable |
| And | 驗證登入頁主標題 | Browser.Get Text | text=高速且穩定的 AI 檢測解決方案 | | == | 高速且穩定的 AI 檢測解決方案 |
| And | 驗證登入頁電子郵件欄位標題 | Browser.Get Text | text=電子郵件 | | == | 電子郵件 |
| And | 驗證登入頁密碼欄位標題 | Browser.Get Text | text=密碼 | | == | 密碼 |
| When | 輸入 Fastable 帳號 | Browser.Fill Text | #username | %{FASTABLE_USERNAME} | | |
| And | 輸入 Fastable 密碼 | Browser.Fill Text | #password | %{FASTABLE_PASSWORD} | | |
| And | 點擊登入按鈕 | Browser.Click | text=登入 | | | |
| And | 等待登入後選單顯示 | Wait For Elements State | text=首頁 | visible ;; timeout=20s | | |
| When | 直接前往掃描頁面 | Go To | http://192.168.1.103/#/barcode | | | |
| And | 等待掃描頁面提示顯示 | Wait For Elements State | text=輸入掃描檢測編號以新增箔卷 | visible ;; timeout=20s | | |
| Then | 驗證掃描頁面選單名稱 | Browser.Get Text | xpath=(//strong[normalize-space()='掃描頁面'])[1] | | == | 掃描頁面 |
| And | 驗證掃描頁面提示內容 | Browser.Get Text | text=輸入掃描檢測編號以新增箔卷 | | == | 輸入掃描檢測編號以新增箔卷 |
| And | 驗證掃描頁面廠房欄位 | Browser.Get Text | text=廠房 | | == | 廠房 |
| And | 驗證掃描頁面產線欄位 | Browser.Get Text | text=產線 | | == | 產線 |
| And | 驗證掃描頁面設定版號欄位 | Browser.Get Text | text=設定版號 | | == | 設定版號 |
| And | 驗證掃描頁面卷號欄位 | Browser.Get Text | text=卷號 | | == | 卷號 |
| And | 驗證掃描頁面新增按鈕 | Browser.Get Text | text=新增 | | == | 新增 |
| And | 驗證掃描頁面開始檢測按鈕 | Browser.Get Text | xpath=(//button[normalize-space()='開始檢測'])[1] | | contains | 開始檢測 |
| And | 驗證掃描頁面警示狀態 | Browser.Get Text | text=警示狀態 ：等待檢測中... | | == | 警示狀態 ：等待檢測中... |
| When | 切換到首頁 | Browser.Click | text=首頁 | | | |
| And | 等待首頁標題出現 | Wait For Elements State | text=Fastable = Fast + Stable | visible ;; timeout=15s | | |
| Then | 驗證首頁品牌標語 | Browser.Get Text | text=Fastable = Fast + Stable | | == | Fastable = Fast + Stable |
| And | 驗證首頁主標題 | Browser.Get Text | text=高速且穩定的 AI 檢測解決方案 | | == | 高速且穩定的 AI 檢測解決方案 |
| When | 切換到即時監控頁 | Browser.Click | text=即時監控 | | | |
| And | 等待即時監控空畫面提示出現 | Wait For Elements State | xpath=(//*[normalize-space()='瑕疵統計'])[1] | visible ;; timeout=20s | | |
| Then | 驗證即時監控頁統計標題 | Browser.Get Text | xpath=(//*[normalize-space()='瑕疵統計'])[1] | | == | 瑕疵統計 |
| And | 驗證即時監控頁空畫面提示 | Browser.Get Text | text=暫無影像 | | == | 暫無影像 |
| And | 驗證即時監控頁卷號資訊 | Browser.Get Text | xpath=(//h4[contains(normalize-space(),'卷號:')])[1] | | contains | 卷號: |
| And | 驗證即時監控頁開始時間資訊 | Browser.Get Text | xpath=(//h4[contains(normalize-space(),'開始時間:')])[1] | | contains | 開始時間: |
| And | 驗證即時監控頁即時碼數資訊 | Browser.Get Text | xpath=(//h4[contains(normalize-space(),'即時碼數:')])[1] | | contains | 即時碼數: |
| When | 切換到調閱頁面 | Browser.Click | text=調閱頁面 | | | |
| And | 等待調閱頁查詢條件顯示 | Wait For Elements State | text=根據品管代號 | visible ;; timeout=15s | | |
| Then | 驗證調閱頁依產線頁籤 | Browser.Get Text | text=根據產線 | | == | 根據產線 |
| And | 驗證調閱頁依品管代號頁籤 | Browser.Get Text | text=根據品管代號 | | == | 根據品管代號 |
| And | 驗證調閱頁依工單號碼頁籤 | Browser.Get Text | text=根據工單號碼 | | == | 根據工單號碼 |
| And | 驗證調閱頁開始日期欄位 | Browser.Get Text | text=開始日期 | | == | 開始日期 |
| And | 驗證調閱頁結束日期欄位 | Browser.Get Text | text=結束日期 | | == | 結束日期 |
| And | 驗證調閱頁搜尋按鈕 | Browser.Get Text | xpath=(//*[normalize-space()='結束日期']/following::button[normalize-space()='搜尋'])[1] | | == | 搜尋 |
| When | 切換到邊緣設定頁 | Browser.Click | text=邊緣設定 | | | |
| And | 等待邊緣設定欄位顯示 | Wait For Elements State | text=左側: | visible ;; timeout=15s | | |
| Then | 驗證邊緣設定頁左側欄位 | Browser.Get Text | text=左側: | | == | 左側: |
| And | 驗證邊緣設定頁右側欄位 | Browser.Get Text | text=右側: | | == | 右側: |
| And | 驗證邊緣設定頁內縮欄位 | Browser.Get Text | text=內縮: | | == | 內縮: |
| And | 驗證邊緣設定頁儲存按鈕 | Browser.Get Text | text=儲存 | | == | 儲存 |
| And | 驗證邊緣設定頁手動設定按鈕 | Browser.Get Text | text=手動設定 | | == | 手動設定 |
| When | 切換到設定頁面 | Browser.Click | text=設定頁面 | | | |
| And | 等待設定頁頁籤顯示 | Wait For Elements State | text=即時警報規則 | visible ;; timeout=20s | | |
| Then | 驗證設定頁即時警報規則頁籤 | Browser.Get Text | text=即時警報規則 | | == | 即時警報規則 |
| And | 驗證設定頁驗報顯示頁籤 | Browser.Get Text | text=驗報顯示 | | == | 驗報顯示 |
| And | 驗證設定頁單頁顯示筆數文字 | Browser.Get Text | xpath=(//*[normalize-space()='單頁顯示筆數 :'])[1] | | contains | 單頁顯示筆數 : |
| And | 驗證設定頁切換模式文字 | Browser.Get Text | text=切換模式 | | == | 切換模式 |
| And | 驗證設定頁表格欄位種類 | Browser.Get Text | text=種類 | | == | 種類 |
| And | 驗證設定頁表格欄位是否警報 | Browser.Get Text | text=是否警報 | | == | 是否警報 |
| And | 驗證設定頁表格欄位長邊 | Browser.Get Text | text=長邊(mm) | | == | 長邊(mm) |
| And | 驗證設定頁表格欄位更新時間 | Browser.Get Text | text=更新時間 | | == | 更新時間 |
| And | 關閉瀏覽器 | Close Browser | | | | |

## 使用說明

- 此檔依目前站點實際頁面與 DOM 快照生成，已符合本專案 Fastable Markdown 測試格式。
- 帳號與密碼改用環境變數 %{FASTABLE_USERNAME} 與 %{FASTABLE_PASSWORD}，執行前請先設定。
- 設定頁面目前可見前端錯誤事件，因此案例只驗證穩定可見的頁籤與表格欄位，不驗證易波動的資料列內容。
- 即時監控頁中的卷號、開始時間、即時碼數會隨資料變動，因此改用 contains 驗證固定前綴文字。
- 若要降低維護成本，可改跑同資料夾下拆分後的頁面測試檔；本檔可保留做整體 smoke 驗證。
