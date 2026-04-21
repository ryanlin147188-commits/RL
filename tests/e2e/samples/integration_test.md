# Integration Test

Resource: ../resources/core_keywords.resource
Test Case: 驗證 Web、Header、JSON 規則與條件查找
Documentation: 這是一個展示 Web 與 API 混合測試進階驗證能力的範例腳本

| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |
| --- | --- | --- | --- | --- | --- | --- |
| Given | 建立Header測試連線 | Create Session | headersession | https://httpbin.org ;; verify=${TRUE} |  |  |
| And | 取得Header測試回應 | GET On Session | headersession | /response-headers?Vary=Accept%2CAccept-Encoding&X-Demo=alpha |  |  |
| Then | 驗證HTTP狀態碼 | 取得 HTTP 狀態碼 |  |  | == | 200 |
| And | 驗證Header存在 | 檢查 HTTP Header 是否存在 | Vary |  | == | True |
| And | 驗證多值Header內容 | 取得 HTTP Header 值 | Vary |  | contains | Accept-Encoding |
| Given | 建立空值測試連線 | Create Session | emptysession | https://httpbin.org ;; verify=${TRUE} |  |  |
| And | 取得空值測試回應 | GET On Session | emptysession | /anything?timestamp=2026-04-19T12:34:56%2B00:00&code=ORD-123-XYZ |  |  |
| Then | 驗證空物件型別 | 取得 JSON 欄位型別 | args |  | == | object |
| And | 驗證欄位不是空 | 檢查 JSON 欄位是否為空 | args |  | == | False |
| And | 驗證代碼符合正則 | 檢查 JSON 欄位符合正則 | args.code | ^ORD-[0-9]{3}-[A-Z]{3}$ | == | True |
| And | 驗證時間欄位格式 | 檢查 JSON 欄位日期格式 | args.timestamp | iso8601 | == | True |
| And | 驗證Body欄位集合 | 檢查 JSON 欄位集合 | args,data,files,form,headers,json,method,origin,url | exact | == | True |
| And | 驗證Body簡化Schema | 檢查 JSON 簡化Schema |  | schema_rules=required=args,headers,method,url; type=object; type.args=object; type.headers=object; type.method=string; type.url=string | == | True |
| Given | 建立API資料連線 | Create Session | mysession | https://jsonplaceholder.typicode.com ;; verify=${TRUE} |  |  |
| And | 發送GET請求 | GET On Session | mysession | /posts |  |  |
| Then | 驗證JSON根層型別 | 取得 JSON 欄位型別 |  |  | == | array |
| And | 驗證第1筆型別 | 取得 JSON 欄位型別 | [0] |  | == | object |
| And | 驗證id型別 | 取得 JSON 欄位型別 | [0].id |  | == | number |
| And | 驗證title型別 | 取得 JSON 欄位型別 | [0].title |  | == | string |
| And | 驗證id是正數 | 檢查 JSON 欄位型別加值 | [0].id | number ;; > 0 | == | True |
| And | 驗證JSON陣列長度 | 取得 JSON 陣列長度 |  |  | == | 100 |
| And | 驗證欄位不存在 | 檢查 JSON 欄位是否存在 | [0].subtitle |  | == | False |
| And | 驗證欄位不存在或為空 | 檢查 JSON 欄位不存在或為空 | [0].subtitle |  | == | True |
| And | 驗證條件式項目存在 | 依條件檢查 JSON 陣列項目是否存在 | userId == 1 |  | == | True |
| And | 驗證排序後前3筆數量 | 依條件取得 JSON 陣列結果數量 | userId == 1 | query_options=sort_by=id;order=desc;top_n=3 | == | 3 |
| And | 驗證排序後前三筆title清單 | 依條件取得 JSON 陣列所有欄位值 | userId == 1 | title ;; query_options=sort_by=id;order=desc;top_n=3 | contains | nesciunt iure omnis dolorem tempora et accusantium |
| And | 驗證排序後第一筆title | 依條件取得 JSON 陣列欄位值 | userId == 1 | [0].title ;; query_options=sort_by=id;order=desc;top_n=3 | == | optio molestias id quia eum |
| When | 開啟無頭瀏覽器 | New Browser | chromium | headless=${TRUE} |  |  |
| And | 前往測試網站 | New Page | https://example.com |  |  |  |
| Then | 驗證網頁大標題 | Browser.Get Text | h1 |  | == | Example Domain |