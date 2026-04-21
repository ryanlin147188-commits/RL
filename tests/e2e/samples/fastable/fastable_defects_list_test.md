# Fastable Defects List Test

Resource: ../../resources/core_keywords.resource
Test Case: 驗證 Fastable 設定頁頁籤切換與表格欄位
Documentation: 登入後前往設定頁面，驗證目前實際存在的即時警報規則與驗報顯示兩個子頁籤、版號下拉選單，以及分頁切換後的表格內容。

| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |
| --- | --- | --- | --- | --- | --- | --- |
| Given | 開啟瀏覽器 | New Browser | chromium | headless=${TRUE} | | |
| And | 前往 Fastable 登入頁 | New Page | http://192.168.1.103/#/fastable/login | | | |
| And | 等待登入帳號欄位顯示 | Wait For Elements State | #username | visible ;; timeout=15s | | |
| When | 輸入 Fastable 帳號 | Browser.Fill Text | #username | %{FASTABLE_USERNAME} | | |
| And | 輸入 Fastable 密碼 | Browser.Fill Text | #password | %{FASTABLE_PASSWORD} | | |
| And | 點擊登入按鈕 | Browser.Click | text=登入 | | | |
| And | 等待登入後選單顯示 | Wait For Elements State | text=首頁 | visible ;; timeout=20s | | |
| When | 直接前往設定頁面 | Go To | http://192.168.1.103/#/defects-list | | | |
| And | 等待設定頁頁籤顯示 | Wait For Elements State | text=即時警報規則 | visible ;; timeout=20s | | |
| Then | 驗證設定頁提示內容 | Browser.Get Text | text=編輯和檢視瑕疵設定 | | == | 編輯和檢視瑕疵設定 |
| And | 驗證即時警報規則頁籤 | Browser.Get Text | text=即時警報規則 | | == | 即時警報規則 |
| And | 驗證驗報顯示頁籤 | Browser.Get Text | text=驗報顯示 | | == | 驗報顯示 |
| And | 等待即時警報規則內容載入 | Wait For Elements State | text=是否警報 | visible ;; timeout=20s | | |
| Then | 驗證即時警報規則欄位種類 | Browser.Get Text | text=種類 | | == | 種類 |
| And | 驗證即時警報規則欄位是否警報 | Browser.Get Text | text=是否警報 | | == | 是否警報 |
| And | 驗證驗報顯示欄位長邊 | Browser.Get Text | text=長邊(mm) | | == | 長邊(mm) |
| And | 驗證即時警報規則欄位更新時間 | Browser.Get Text | text=更新時間 | | == | 更新時間 |
| And | 驗證設定頁切換模式文字 | Browser.Get Text | text=切換模式 | | == | 切換模式 |
| And | 驗證設定頁單頁顯示筆數文字 | Browser.Get Text | xpath=(//*[normalize-space()='單頁顯示筆數 :'])[1] | | contains | 單頁顯示筆數 : |
| When | 展開版號下拉選單 | Browser.Click | xpath=(//*[normalize-space()='default_staging_copy' or normalize-space()='default_staging2' or normalize-space()='default'])[1] | | | |
| And | 等待版號選項顯示 | Wait For Elements State | xpath=(//*[normalize-space()='default_staging2'])[last()] | visible ;; timeout=15s | | |
| Then | 驗證版號選項 default_staging2 | Browser.Get Text | xpath=(//*[normalize-space()='default_staging2'])[last()] | | == | default_staging2 |
| And | 驗證版號選項 default_staging_copy | Browser.Get Text | xpath=(//*[normalize-space()='default_staging_copy'])[last()] | | == | default_staging_copy |
| And | 驗證版號選項 default | Browser.Get Text | xpath=(//*[normalize-space()='default'])[last()] | | == | default |
| When | 切換版號到 default_staging_copy | Browser.Click | xpath=(//*[normalize-space()='default_staging_copy'])[last()] | | | |
| And | 等待版號文字更新 | Wait For Elements State | xpath=(//div[normalize-space()='default_staging_copy'])[1] | visible ;; timeout=20s | | |
| Then | 驗證目前版號文字 | Browser.Get Text | xpath=(//div[normalize-space()='default_staging_copy'])[1] | | == | default_staging_copy |
| And | 驗證切換後首筆資料關鍵字 | Browser.Get Text | text=破絲 | | == | 破絲 |
| When | 切換到驗報顯示頁籤 | Browser.Click | text=驗報顯示 | | | |
| And | 等待驗報顯示內容載入 | Wait For Elements State | text=是否顯示 | visible ;; timeout=20s | | |
| Then | 驗證驗報顯示欄位種類 | Browser.Get Text | text=種類 | | == | 種類 |
| And | 驗證驗報顯示欄位是否顯示 | Browser.Get Text | text=是否顯示 | | == | 是否顯示 |
| And | 驗證驗報顯示欄位長邊 | Browser.Get Text | text=長邊(mm) | | == | 長邊(mm) |
| And | 驗證驗報顯示欄位更新時間 | Browser.Get Text | text=更新時間 | | == | 更新時間 |
| When | 切換回即時警報規則頁籤 | Browser.Click | text=即時警報規則 | | | |
| And | 等待即時警報規則欄位再度顯示 | Wait For Elements State | text=是否警報 | visible ;; timeout=20s | | |
| Then | 驗證切回即時警報規則成功 | Browser.Get Text | text=是否警報 | | == | 是否警報 |
| When | 點擊第二頁分頁 | Browser.Click | xpath=(//a[normalize-space()='2'])[1] | | | |
| And | 等待第二頁資料列顯示 | Wait For Elements State | text=縮碼痕 | visible ;; timeout=20s | | |
| Then | 驗證第二頁第一批資料關鍵字 | Browser.Get Text | text=縮碼痕 | | == | 縮碼痕 |
| And | 驗證第二頁存在上一頁按鈕 | Browser.Get Text | xpath=(//a[normalize-space()='<'])[1] | | == | < |
| And | 關閉瀏覽器 | Close Browser | | | | |