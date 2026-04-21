# Fastable Query Page Test

Resource: ../../resources/core_keywords.resource
Test Case: 驗證 Fastable 調閱頁支援本地JSON欄位對照
Documentation: 登入後前往調閱頁面，載入 lot 與 images 本地 JSON 樣本，使用工單號碼查詢穩定進入詳細資訊頁，並驗證當卷資訊與瑕疵分佈圖數據可與 JSON 對照。

| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |
| --- | --- | --- | --- | --- | --- | --- |
| Given | 開啟瀏覽器 | New Browser | chromium | headless=${TRUE} | | |
| And | 前往 Fastable 登入頁 | New Page | http://192.168.1.103/#/fastable/login | | | |
| And | 等待登入帳號欄位顯示 | Wait For Elements State | #username | visible ;; timeout=15s | | |
| When | 輸入 Fastable 帳號 | Browser.Fill Text | #username | %{FASTABLE_USERNAME} | | |
| And | 輸入 Fastable 密碼 | Browser.Fill Text | #password | %{FASTABLE_PASSWORD} | | |
| And | 點擊登入按鈕 | Browser.Click | text=登入 | | | |
| And | 等待登入後選單顯示 | Wait For Elements State | text=首頁 | visible ;; timeout=20s | | |
| When | 直接前往調閱頁面 | Go To | http://192.168.1.103/#/barcode-query | | | |
| And | 等待調閱頁查詢頁籤顯示 | Wait For Elements State | text=根據工單號碼 | visible ;; timeout=20s | | |
| And | 重置本地 JSON 資料來源 | 重置本地JSON資料 | | | | |
| And | 載入 lot JSON 樣本 | 載入本地JSON資料 | tests/testdata/fastable/lot_num_17.json | alias=lot_json | | |
| And | 載入 images JSON 樣本 | 載入本地JSON資料 | tests/testdata/fastable/images_data_lot_17.json | alias=images_json | | |
| Then | 驗證產線查詢頁籤存在 | Browser.Get Text | text=根據產線 | | == | 根據產線 |
| And | 驗證品管代號查詢頁籤存在 | Browser.Get Text | text=根據品管代號 | | == | 根據品管代號 |
| And | 驗證工單號碼查詢頁籤存在 | Browser.Get Text | text=根據工單號碼 | | == | 根據工單號碼 |
| When | 切換到工單號碼查詢 | Browser.Click | text=根據工單號碼 | | | |
| And | 等待工單號碼輸入框顯示 | Wait For Elements State | css=input[placeholder='例: M01sh0602K30']:visible | visible ;; timeout=15s | | |
| And | 以 lot JSON 工單號碼填入查詢欄位 | 使用本地JSON欄位值填入頁面欄位 | css=input[placeholder='例: M01sh0602K30']:visible | source_alias=lot_json ;; json_path=data.order_number | | |
| And | 點擊工單號碼搜尋 | Browser.Click | xpath=(//*[normalize-space()='工單號碼:']/following::button[normalize-space()='搜尋'])[1] | | | |
| And | 等待查詢結果第一列其他資訊顯示 | Wait For Elements State | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='工卡號碼']/following-sibling::p)[1] | visible ;; timeout=20s | | |
| Then | 驗證查詢結果表格出現工單號碼 | Browser.Get Text | xpath=(//table//*[normalize-space()='C26020721'])[1] | | == | C26020721 |
| And | 驗證查詢結果工卡號碼對照 lot JSON | 驗證頁面欄位與JSON欄位一致 | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='工卡號碼']/following-sibling::p)[1] | source_alias=lot_json ;; json_path=data.work_number | | |
| And | 驗證查詢結果工單號碼對照 lot JSON | 驗證頁面欄位與JSON欄位一致 | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='工單號碼']/following-sibling::p)[1] | source_alias=lot_json ;; json_path=data.order_number | | |
| And | 驗證查詢結果缸號對照 lot JSON | 驗證頁面欄位與JSON欄位一致 | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='缸號']/following-sibling::p)[1] | source_alias=lot_json ;; json_path=data.vat_number | | |
| And | 驗證查詢結果色名對照 lot JSON | 驗證頁面欄位與JSON欄位一致 | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='色名']/following-sibling::p)[1] | source_alias=lot_json ;; json_path=data.color_name | | |
| And | 驗證查詢結果色號對照 lot JSON | 驗證頁面欄位與JSON欄位一致 | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='色號']/following-sibling::p)[1] | source_alias=lot_json ;; json_path=data.color_number | | |
| And | 驗證查詢結果公斤重對照 lot JSON | 驗證頁面欄位與JSON欄位一致 | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='公斤重']/following-sibling::p)[1] | source_alias=lot_json ;; json_path=data.weight_kg ;; page_normalizer=number ;; json_normalizer=number | | |
| And | 驗證查詢結果訂單幅寬對照 lot JSON | 驗證頁面欄位與JSON欄位一致 | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='訂單幅寬']/following-sibling::p)[1] | source_alias=lot_json ;; json_path=data.order_width ;; page_normalizer=number ;; json_normalizer=number | | |
| And | 驗證查詢結果訂單碼重對照 lot JSON | 驗證頁面欄位與JSON欄位一致 | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='訂單碼重']/following-sibling::p)[1] | source_alias=lot_json ;; json_path=data.order_weight ;; page_normalizer=number ;; json_normalizer=number | | |
| And | 驗證查詢結果品牌item對照 lot JSON | 驗證頁面欄位與JSON欄位一致 | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='品牌item']/following-sibling::p)[1] | source_alias=lot_json ;; json_path=data.brand_item | | |
| And | 驗證查詢結果開發編號對照 lot JSON | 驗證頁面欄位與JSON欄位一致 | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='開發編號']/following-sibling::p)[1] | source_alias=lot_json ;; json_path=data.dev_number | | |
| And | 驗證查詢結果品管代號對照 lot JSON | 驗證頁面欄位與JSON欄位一致 | xpath=((//td[.//*[normalize-space()='C26020721']])[1]//*[normalize-space()='品管代號']/following-sibling::p)[1] | source_alias=lot_json ;; json_path=data.qc | | |
| Then | 驗證 images JSON 總筆數 | 取得本地JSON結果數量 | images_json | array_path=images ;; normalizer=number | == | 12 |
| And | 驗證 images JSON 油污筆數 | 取得本地JSON結果數量 | images_json | array_path=images ;; condition_expression=concept_labels[0] == 油污 ;; normalizer=number | == | 1 |
| And | 驗證 images JSON 棉粒筆數 | 取得本地JSON結果數量 | images_json | array_path=images ;; condition_expression=concept_labels[0] == 棉粒(不扣點) ;; normalizer=number | == | 5 |
| And | 驗證 images JSON 接疋筆數 | 取得本地JSON結果數量 | images_json | array_path=images ;; condition_expression=concept_labels[0] == 接疋(不扣點) ;; normalizer=number | == | 6 |
| And | 關閉瀏覽器 | Close Browser | | | | |
