# Fastable Realtime Alarm Test

Resource: ../../resources/core_keywords.resource
Test Case: 驗證 Fastable 即時監控頁主要資訊
Documentation: 登入後直接前往即時監控頁，驗證統計區塊、空畫面提示與目前實際資訊標題前綴。

| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |
| --- | --- | --- | --- | --- | --- | --- |
| Given | 開啟瀏覽器 | New Browser | chromium | headless=${TRUE} | | |
| And | 前往 Fastable 登入頁 | New Page | http://192.168.1.103/#/fastable/login | | | |
| And | 等待登入帳號欄位顯示 | Wait For Elements State | #username | visible ;; timeout=15s | | |
| When | 輸入 Fastable 帳號 | Browser.Fill Text | #username | %{FASTABLE_USERNAME} | | |
| And | 輸入 Fastable 密碼 | Browser.Fill Text | #password | %{FASTABLE_PASSWORD} | | |
| And | 點擊登入按鈕 | Browser.Click | text=登入 | | | |
| And | 等待登入後選單顯示 | Wait For Elements State | text=首頁 | visible ;; timeout=20s | | |
| When | 直接前往即時監控頁 | Go To | http://192.168.1.103/#/realtime-alarm | | | |
| And | 等待即時監控頁載入 | Wait For Elements State | xpath=(//*[normalize-space()='瑕疵統計'])[1] | visible ;; timeout=20s | | |
| Then | 驗證即時監控頁統計標題 | Browser.Get Text | xpath=(//*[normalize-space()='瑕疵統計'])[1] | | == | 瑕疵統計 |
| And | 驗證即時監控頁空畫面提示 | Browser.Get Text | text=暫無影像 | | == | 暫無影像 |
| And | 驗證即時監控頁卷號資訊 | Browser.Get Text | xpath=(//h4[contains(normalize-space(),'卷號:')])[1] | | contains | 卷號: |
| And | 驗證即時監控頁開始時間資訊 | Browser.Get Text | xpath=(//h4[contains(normalize-space(),'開始時間:')])[1] | | contains | 開始時間: |
| And | 驗證即時監控頁即時碼數資訊 | Browser.Get Text | xpath=(//h4[contains(normalize-space(),'即時碼數:')])[1] | | contains | 即時碼數: |
| And | 驗證即時監控頁即時幅寬資訊 | Browser.Get Text | xpath=(//h4[contains(normalize-space(),'即時幅寬:')])[1] | | contains | 即時幅寬: |
| And | 關閉瀏覽器 | Close Browser | | | | |