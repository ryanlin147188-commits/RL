# PChome Login Test

Resource: ../resources/core_keywords.resource
Test Case: 驗證 PChome 登入頁基本元素與空白送出提示
Documentation: 驗證 PChome 登入頁可正常開啟，且空白送出時會顯示帳號欄位提示訊息

| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |
| --- | --- | --- | --- | --- | --- | --- |
| Given | 開啟瀏覽器 | New Browser | chromium | headless=${FALSE} | | |
| And | 前往 PChome 登入頁 | New Page | https://ecvip.pchome.com.tw/login/v3/login.htm?rurl=https%3A%2F%2F24h.pchome.com.tw%2F&mrg=1 | | | |
| Then | 驗證站台品牌標題 | Browser.Get Text | .Ht h1.logotype | | == | PChome 24h購物 |
| And | 驗證登入歡迎文字 | Browser.Get Text | div.c-auth__headLine >> nth=0 | | == | 歡迎登入 |
| And | 驗證立即註冊連結 | Browser.Get Text | a[id="goSignUp"] | | == | 立即註冊 |
| And | 驗證繼續按鈕文字 | Browser.Get Text | button[id="btnKeep"] .btn__text | | == | 繼續 |
| And | 點擊繼續按鈕 | Browser.Click | button[id="btnKeep"] | | | |
| Then | 驗證空白送出提示 | Browser.Get Text | div[id="loginAccErr"] | | == | 請輸入手機號碼 或 Email |
| And | 驗證快速登入區塊文字 | Browser.Get Text | div.c-auth__tite | | == | 使用以下帳號快速登入 |