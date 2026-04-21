# JSONPlaceholder API Test

Resource: ../resources/core_keywords.resource
Test Case: 驗證 JSONPlaceholder 文章與留言 API 回應
Documentation: 驗證單筆文章與文章留言 API 的狀態碼、Header、JSON 結構、欄位內容與條件查找結果

| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |
| --- | --- | --- | --- | --- | --- | --- |
| Given | 建立文章API連線 | Create Session | postsession | https://jsonplaceholder.typicode.com ;; verify=${TRUE} | | |
| When | 查詢單筆文章 | GET On Session | postsession | /posts/1 | | |
| Then | 驗證文章狀態碼 | 取得 HTTP 狀態碼 | | | == | 200 |
| And | 驗證文章Content-Type存在 | 檢查 HTTP Header 是否存在 | Content-Type | | == | True |
| And | 驗證文章Content-Type格式 | 取得 HTTP Header 值 | Content-Type | | contains | application/json |
| And | 驗證文章根層型別 | 取得 JSON 欄位型別 | | | == | object |
| And | 驗證文章必要欄位集合 | 檢查 JSON 欄位集合 | userId,id,title,body | exact | == | True |
| And | 驗證文章Schema | 檢查 JSON 簡化Schema | | schema_rules=required=userId,id,title,body; type=object; type.userId=number; type.id=number; type.title=string; type.body=string | == | True |
| And | 驗證文章id是正數 | 檢查 JSON 欄位型別加值 | id | number ;; > 0 | == | True |
| And | 驗證文章標題內容 | 取得 JSON 欄位值 | title | | == | sunt aut facere repellat provident occaecati excepturi optio reprehenderit |
| And | 驗證文章內容包含關鍵字 | 取得 JSON 欄位值 | body | | contains | suscipit |
| And | 驗證文章不存在subtitle欄位 | 檢查 JSON 欄位是否存在 | subtitle | | == | False |
| When | 查詢文章留言清單 | GET On Session | postsession | /comments?postId=1 | | |
| Then | 驗證留言清單根層型別 | 取得 JSON 欄位型別 | | | == | array |
| And | 驗證留言清單筆數 | 取得 JSON 陣列長度 | | | == | 5 |
| And | 驗證第一筆留言欄位集合 | 檢查 JSON 欄位集合 | postId,id,name,email,body | exact ;; json_path=[0] | == | True |
| And | 驗證第一筆留言email格式 | 檢查 JSON 欄位符合正則 | [0].email | ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}$ | == | True |
| And | 驗證留言條件項目存在 | 依條件檢查 JSON 陣列項目是否存在 | id == 2 | | == | True |
| And | 驗證條件留言email | 依條件取得 JSON 陣列欄位值 | id == 2 | [0].email | == | Jayne_Kuhic@sydney.com |
| And | 驗證前兩筆留言email清單 | 依條件取得 JSON 陣列所有欄位值 | postId == 1 | email ;; query_options=sort_by=id;order=asc;top_n=2 | contains | Jayne_Kuhic@sydney.com |