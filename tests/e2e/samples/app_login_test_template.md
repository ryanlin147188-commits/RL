# App Login Test Template

Resource: ../resources/core_keywords.resource
Test Case: 驗證 APP 登入頁基本元素與空白送出提示
Documentation: 此檔為 Appium APP 測試模板。請先啟動 Appium Server，並將 appPackage、appActivity、元素 locator 與預期文字改成你的 APP 實際值後再執行。

| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |
| --- | --- | --- | --- | --- | --- | --- |
| Given | 連線 Appium 並啟動 APP | Open Application | http://127.0.0.1:4723 | platformName=Android ;; automationName=UiAutomator2 ;; deviceName=Android Emulator ;; appPackage=your.app.package ;; appActivity=your.app.activity ;; noReset=${FALSE} ;; autoGrantPermissions=${TRUE} | | |
| And | 等待登入頁載入 | Wait Until Page Contains Element | id=your.app.package:id/login_title | 15s | | |
| Then | 驗證登入頁標題 | Get Text | id=your.app.package:id/login_title | | == | 會員登入 |
| And | 驗證登入按鈕文字 | Get Text | id=your.app.package:id/login_button | | == | 登入 |
| When | 點擊登入按鈕但不輸入資料 | Click Element | id=your.app.package:id/login_button | | | |
| Then | 驗證帳號錯誤訊息 | Get Text | id=your.app.package:id/username_error | | == | 請輸入帳號 |
| And | 驗證密碼錯誤訊息 | Get Text | id=your.app.package:id/password_error | | == | 請輸入密碼 |
| When | 輸入帳號 | Input Text | id=your.app.package:id/username_input | demo_user | | |
| And | 輸入密碼 | Input Text | id=your.app.package:id/password_input | demo_password | | |
| And | 再次點擊登入 | Click Element | id=your.app.package:id/login_button | | | |
| And | 等待首頁載入 | Wait Until Page Contains Element | id=your.app.package:id/home_title | 15s | | |
| Then | 驗證首頁標題 | Get Text | id=your.app.package:id/home_title | | == | 首頁 |
| And | 關閉 APP | Close Application | | | | |