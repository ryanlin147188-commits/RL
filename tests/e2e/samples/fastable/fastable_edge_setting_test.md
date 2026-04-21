# Fastable Edge Setting Test

Resource: ../../resources/core_keywords.resource
Test Case: 驗證 Fastable 邊緣設定頁主要欄位
Documentation: 登入後前往邊緣設定頁，驗證目前實際可見的邊界設定欄位、操作按鈕與更新時間資訊。

| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |
| --- | --- | --- | --- | --- | --- | --- |
| Given | 開啟瀏覽器 | New Browser | chromium | headless=${TRUE} | | |
| And | 前往 Fastable 登入頁 | New Page | http://192.168.1.103/#/fastable/login | | | |
| And | 等待登入帳號欄位顯示 | Wait For Elements State | #username | visible ;; timeout=15s | | |
| When | 輸入 Fastable 帳號 | Browser.Fill Text | #username | %{FASTABLE_USERNAME} | | |
| And | 輸入 Fastable 密碼 | Browser.Fill Text | #password | %{FASTABLE_PASSWORD} | | |
| And | 點擊登入按鈕 | Browser.Click | text=登入 | | | |
| And | 等待登入後選單顯示 | Wait For Elements State | text=首頁 | visible ;; timeout=20s | | |
| When | 直接前往邊緣設定頁 | Go To | http://192.168.1.103/#/edge-setting | | | |
| And | 等待邊緣設定欄位顯示 | Wait For Elements State | text=左側: | visible ;; timeout=20s | | |
| Then | 驗證邊緣設定頁左側欄位 | Browser.Get Text | text=左側: | | == | 左側: |
| And | 驗證邊緣設定頁右側欄位 | Browser.Get Text | text=右側: | | == | 右側: |
| And | 驗證邊緣設定頁內縮欄位 | Browser.Get Text | text=內縮: | | == | 內縮: |
| And | 驗證邊緣設定頁儲存按鈕 | Browser.Get Text | text=儲存 | | == | 儲存 |
| And | 驗證邊緣設定頁手動設定按鈕 | Browser.Get Text | text=手動設定 | | == | 手動設定 |
| And | 驗證邊緣設定頁更新時間 | Browser.Get Text | xpath=(//*[contains(.,'更新時間:')])[last()] | | contains | 更新時間: |
| And | 關閉瀏覽器 | Close Browser | | | | |