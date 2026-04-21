*** Settings ***
Library    Browser
Library    RequestsLibrary
Library    DatabaseLibrary
Library    AppiumLibrary
Library    Collections
Library    String
Library    api_json_helper.py

*** Variables ***
${BROWSER_AUTO_WAIT_TIMEOUT}    10s
${STEP_HIGHLIGHT_DURATION}    400ms
${STEP_HIGHLIGHT_WIDTH}    4px
${STEP_HIGHLIGHT_STYLE}    solid
${STEP_HIGHLIGHT_COLOR}    red
${AUTO_CLOSE_POPUP_RETRY_COUNT}    3
${AUTO_CLOSE_POPUP_WAIT}    200ms

*** Keywords ***
執行通用測試步驟
    [Arguments]    ${bdd}    ${step_desc}    ${action}    ${target}=${EMPTY}    ${input_data}=${EMPTY}    ${operator}=${EMPTY}    ${expected}=${EMPTY}
    ${step_index}=    取得下一個步驟序號
    Log    執行步驟: [${bdd}] ${step_desc}

    ${is_browser_action}=    是否為瀏覽器動作    ${action}
    ${selector_target}=    取得瀏覽器定位目標    ${action}    ${target}
    ${selector_target}=    正規化瀏覽器定位目標    ${selector_target}
    ${effective_target}=    Set Variable If    $selector_target != ''    ${selector_target}    ${target}

    ${action_values}=    解析步驟輸入    ${effective_target}    ${input_data}
    ${preserve_first_arg}=    Evaluate    $effective_target != ''
    ${args}    ${named_args}=    建立執行參數    ${preserve_first_arg}    @{action_values}

    執行前自動等待    ${is_browser_action}    ${selector_target}
    擷取步驟前截圖    ${step_index}    ${bdd}    ${step_desc}    ${selector_target}

    ${action_status}    ${result}=    Run Keyword And Ignore Error    ${action}    @{args}    &{named_args}
    ${action_passed}=    Evaluate    $action_status == 'PASS'
    IF    ${action_passed}
        儲存瀏覽器上下文狀態    ${action}    ${result}
        儲存最後API回應    ${result}
    END

    執行後自動等待    ${is_browser_action}
    擷取步驟後截圖    ${step_index}    ${bdd}    ${step_desc}

    IF    not ${action_passed}
        Fail    ${result}
    END

    ${has_assertion}=    Evaluate    $operator != ''
    IF    ${has_assertion}
        驗證步驟結果    ${result}    ${operator}    ${expected}
    END

取得下一個步驟序號
    ${current_index}=    Get Variable Value    ${STEP_SEQUENCE}    0
    ${next_index}=    Evaluate    int($current_index) + 1
    Set Test Variable    ${STEP_SEQUENCE}    ${next_index}
    RETURN    ${next_index}

是否為瀏覽器動作
    [Arguments]    ${action}
    ${normalized_action}=    正規化動作名稱    ${action}
    ${is_browser_prefixed}=    Evaluate    str($action).lower().startswith('browser.')
    ${known_browser_actions}=    Create List
    ...    new browser
    ...    new context
    ...    new page
    ...    go to
    ...    reload
    ...    click
    ...    get text
    ...    fill text
    ...    type text
    ...    clear text
    ...    hover
    ...    dblclick
    ...    double click
    ...    press keys
    ...    scroll to element
    ...    wait for elements state
    ...    evaluate javascript
    ...    take screenshot
    ...    check checkbox
    ...    uncheck checkbox
    ...    select options by
    ...    get element count
    ...    close page
    ...    close browser
    ...    使用本地json欄位值填入頁面欄位
    ...    驗證頁面欄位與json欄位一致
    ...    驗證頁面欄位與json筆數一致
    ${is_known_browser_action}=    Run Keyword And Return Status    List Should Contain Value    ${known_browser_actions}    ${normalized_action}
    ${is_browser_action}=    Evaluate    $is_browser_prefixed or $is_known_browser_action
    RETURN    ${is_browser_action}

正規化動作名稱
    [Arguments]    ${action}
    ${normalized_action}=    Evaluate    str($action).split('.', 1)[1].strip().lower() if str($action).lower().startswith('browser.') else str($action).strip().lower()
    RETURN    ${normalized_action}

取得瀏覽器定位目標
    [Arguments]    ${action}    ${target}=${EMPTY}
    ${normalized_action}=    正規化動作名稱    ${action}
    ${selector_actions}=    Create List
    ...    click
    ...    get text
    ...    fill text
    ...    type text
    ...    clear text
    ...    hover
    ...    dblclick
    ...    double click
    ...    press keys
    ...    scroll to element
    ...    wait for elements state
    ...    evaluate javascript
    ...    take screenshot
    ...    check checkbox
    ...    uncheck checkbox
    ...    select options by
    ...    get element count
    ...    使用本地json欄位值填入頁面欄位
    ...    驗證頁面欄位與json欄位一致
    ...    驗證頁面欄位與json筆數一致
    ${uses_selector}=    Run Keyword And Return Status    List Should Contain Value    ${selector_actions}    ${normalized_action}
    IF    ${uses_selector} and $target != ''
        RETURN    ${target}
    END
    RETURN    ${EMPTY}

正規化瀏覽器定位目標
    [Arguments]    ${target}=${EMPTY}
    IF    $target == ''
        RETURN    ${EMPTY}
    END

    ${has_explicit_prefix}=    Run Keyword And Return Status
    ...    Should Match Regexp
    ...    ${target}
    ...    ^[A-Za-z][A-Za-z0-9+_-]*=
    IF    ${has_explicit_prefix}
        RETURN    ${target}
    END

    ${is_css_selector}=    Run Keyword And Return Status
    ...    Should Match Regexp
    ...    ${target}
    ...    ^[#.\[]
    IF    ${is_css_selector}
        RETURN    css=${target}
    END

    RETURN    ${target}

執行前自動等待
    [Arguments]    ${is_browser_action}    ${selector_target}=${EMPTY}
    IF    not ${is_browser_action}
        RETURN
    END

    ${has_page}=    是否有可用瀏覽器頁面
    IF    not ${has_page}
        RETURN
    END

    設定自動處理瀏覽器原生對話框
    自動關閉頁面彈出視窗
    Run Keyword And Ignore Error    Browser.Wait For Load State    domcontentloaded    timeout=${BROWSER_AUTO_WAIT_TIMEOUT}
    IF    $selector_target != ''
        Run Keyword And Ignore Error    Browser.Wait For Elements State    ${selector_target}    visible    timeout=${BROWSER_AUTO_WAIT_TIMEOUT}
        Run Keyword And Ignore Error    Browser.Scroll To Element    ${selector_target}
    END

執行後自動等待
    [Arguments]    ${is_browser_action}
    IF    not ${is_browser_action}
        RETURN
    END

    ${has_page}=    是否有可用瀏覽器頁面
    IF    not ${has_page}
        RETURN
    END

    Run Keyword And Ignore Error    Browser.Wait For Load State    domcontentloaded    timeout=${BROWSER_AUTO_WAIT_TIMEOUT}
    自動關閉頁面彈出視窗

設定自動處理瀏覽器原生對話框
    Run Keyword And Ignore Error    Browser.Handle Future Dialogs    action=dismiss

自動關閉頁面彈出視窗
    FOR    ${index}    IN RANGE    ${AUTO_CLOSE_POPUP_RETRY_COUNT}
        ${close_status}    ${closed_count}=    Run Keyword And Ignore Error
        ...    Browser.Evaluate JavaScript
        ...    ${NONE}
        ...    () => {
        ...        const getStyle = (element) => window.getComputedStyle(element);
        ...        const isVisible = (element) => {
        ...            if (!element) {
        ...                return false;
        ...            }
        ...            const style = getStyle(element);
        ...            const rect = element.getBoundingClientRect();
        ...            return style.visibility !== 'hidden'
        ...                && style.display !== 'none'
        ...                && rect.width > 0
        ...                && rect.height > 0;
        ...        };
        ...        const isClickable = (element) => {
        ...            if (!isVisible(element) || typeof element.click !== 'function') {
        ...                return false;
        ...            }
        ...            return getStyle(element).pointerEvents !== 'none';
        ...        };
        ...        const normalizeText = (value) => (value || '').replace(/\s+/g, ' ').trim();
        ...        const highPriorityTexts = new Set(['關閉', '取消', '知道了', '我知道了', '略過', '稍後再說', '下次再說']);
        ...        const fallbackTexts = new Set(['確定', '確認', 'OK', 'Ok', 'ok']);
        ...        const prioritizedSelectors = [
        ...            '.swal2-container .swal2-close',
        ...            '.swal2-container .swal2-cancel',
        ...            '.ant-modal-root .ant-modal-close',
        ...            '.ant-notification-notice-close',
        ...            '.ant-message-notice-close',
        ...            '.el-dialog__headerbtn',
        ...            '.el-message-box__headerbtn',
        ...            '.modal.show .btn-close',
        ...            '.modal.show .close',
        ...            '.toast.show .btn-close',
        ...            '.toast.show .close',
        ...            '.Toastify__close-button',
        ...            '[role="dialog"] [aria-label="Close"]',
        ...            '[role="alertdialog"] [aria-label="Close"]',
        ...            '[data-testid="close"]'
        ...        ];
        ...        for (const selector of prioritizedSelectors) {
        ...            for (const element of document.querySelectorAll(selector)) {
        ...                if (isClickable(element)) {
        ...                    element.click();
        ...                    return 1;
        ...                }
        ...            }
        ...        }
        ...        const overlayContainers = [
        ...            ...document.querySelectorAll('[role="dialog"], [role="alertdialog"], .modal.show, .swal2-container, .ant-modal-root, .el-dialog__wrapper, .el-message-box__wrapper, .toast.show, .Toastify, .ant-notification, .ant-message')
        ...        ].filter(isVisible);
        ...        const clickByText = (texts) => {
        ...            for (const container of overlayContainers) {
        ...                const buttons = container.querySelectorAll('button, [role="button"], .btn');
        ...                for (const button of buttons) {
        ...                    if (!isClickable(button)) {
        ...                        continue;
        ...                    }
        ...                    if (texts.has(normalizeText(button.innerText))) {
        ...                        button.click();
        ...                        return true;
        ...                    }
        ...                }
        ...            }
        ...            return false;
        ...        };
        ...        if (clickByText(highPriorityTexts)) {
        ...            return 1;
        ...        }
        ...        for (const container of overlayContainers) {
        ...            const buttons = [...container.querySelectorAll('button, [role="button"], .btn')].filter(isClickable);
        ...            if (buttons.length === 1 && fallbackTexts.has(normalizeText(buttons[0].innerText))) {
        ...                buttons[0].click();
        ...                return 1;
        ...            }
        ...        }
        ...        return 0;
        ...    }
        IF    '${close_status}' != 'PASS'
            RETURN
        END
        ${has_closed_popup}=    Evaluate    int($closed_count) > 0
        IF    not ${has_closed_popup}
            RETURN
        END
        Log    已自動關閉頁面彈出視窗
        Sleep    ${AUTO_CLOSE_POPUP_WAIT}
    END

擷取步驟前截圖
    [Arguments]    ${step_index}    ${bdd}    ${step_desc}    ${selector_target}=${EMPTY}
    ${has_page}=    是否有可用瀏覽器頁面
    IF    not ${has_page}
        RETURN
    END

    ${filename}=    產生步驟截圖檔名    ${step_index}    before
    IF    $selector_target != ''
        Run Keyword And Ignore Error    Browser.Highlight Elements    ${selector_target}    duration=${STEP_HIGHLIGHT_DURATION}    width=${STEP_HIGHLIGHT_WIDTH}    style=${STEP_HIGHLIGHT_STYLE}    color=${STEP_HIGHLIGHT_COLOR}    mode=border
    END
    ${screenshot_status}    ${screenshot_path}=    Run Keyword And Ignore Error    Browser.Take Screenshot    ${filename}
    IF    $selector_target != ''
        Sleep    ${STEP_HIGHLIGHT_DURATION}
    END

擷取步驟後截圖
    [Arguments]    ${step_index}    ${bdd}    ${step_desc}
    ${has_page}=    是否有可用瀏覽器頁面
    IF    not ${has_page}
        RETURN
    END

    ${filename}=    產生步驟截圖檔名    ${step_index}    after
    ${screenshot_status}    ${screenshot_path}=    Run Keyword And Ignore Error    Browser.Take Screenshot    ${filename}

產生步驟截圖檔名
    [Arguments]    ${step_index}    ${phase}
    ${test_name}=    Get Variable Value    ${TEST NAME}    test
    ${test_hash}=    Evaluate    __import__('hashlib').md5(str($test_name).encode('utf-8')).hexdigest()[:8]
    ${formatted_index}=    Evaluate    f'{int($step_index):03d}'
    RETURN    ${test_hash}_step_${formatted_index}_${phase}

是否有可用瀏覽器頁面
    ${has_page}=    Get Variable Value    ${HAS_ACTIVE_BROWSER_PAGE}    ${FALSE}
    RETURN    ${has_page}

儲存瀏覽器上下文狀態
    [Arguments]    ${action}    ${result}
    ${normalized_action}=    正規化動作名稱    ${action}
    IF    '${normalized_action}' == 'new page'
        Set Test Variable    ${HAS_ACTIVE_BROWSER_PAGE}    ${TRUE}
    ELSE IF    '${normalized_action}' == 'close page'
        Set Test Variable    ${HAS_ACTIVE_BROWSER_PAGE}    ${FALSE}
    ELSE IF    '${normalized_action}' == 'close browser'
        Set Test Variable    ${HAS_ACTIVE_BROWSER_PAGE}    ${FALSE}
    END

解析步驟輸入
    [Arguments]    ${target}=${EMPTY}    ${input_data}=${EMPTY}
    ${raw_values}=    Create List
    IF    $target != ''
        Append To List    ${raw_values}    ${target}
    END
    IF    $input_data != ''
        ${parts}=    Split String    ${input_data}    ;;
        FOR    ${value}    IN    @{parts}
            ${trimmed}=    Strip String    ${value}
            IF    $trimmed != ''
                Append To List    ${raw_values}    ${trimmed}
            END
        END
    END
    RETURN    ${raw_values}

建立執行參數
    [Arguments]    ${preserve_first_arg}=${FALSE}    @{raw_values}
    ${args}=    Create List
    ${named_args}=    Create Dictionary
    ${preserve_next_value}=    Set Variable    ${preserve_first_arg}
    FOR    ${value}    IN    @{raw_values}
        IF    $value != ''
            IF    ${preserve_next_value}
                ${arg_value}=    轉換參數值    ${value}
                Append To List    ${args}    ${arg_value}
                ${preserve_next_value}=    Set Variable    ${FALSE}
                CONTINUE
            END
            ${is_named_arg}=    Run Keyword And Return Status
            ...    Should Match Regexp
            ...    ${value}
            ...    ^[A-Za-z_][A-Za-z0-9_]*=
            IF    ${is_named_arg}
                ${parts}=    Split String    ${value}    =    1
                ${arg_name}=    Set Variable    ${parts}[0]
                ${arg_value}=    轉換參數值    ${parts}[1]
                Evaluate    $named_args.update({$arg_name: $arg_value})
            ELSE
                ${arg_value}=    轉換參數值    ${value}
                Append To List    ${args}    ${arg_value}
            END
        END
    END
    RETURN    ${args}    ${named_args}

儲存最後API回應
    [Arguments]    ${result}
    ${is_response}=    Evaluate    hasattr($result, 'status_code') and callable(getattr($result, 'json', None))
    IF    ${is_response}
        Set Test Variable    ${LAST_RESPONSE}    ${result}
    END

取得最後API回應
    ${response}=    Get Variable Value    ${LAST_RESPONSE}    ${NONE}
    IF    $response is $NONE
        Fail    找不到可供驗證的 API 回應。請先執行 HTTP 請求步驟。
    END
    RETURN    ${response}

取得 HTTP 狀態碼
    [Arguments]    ${unused}=${EMPTY}
    ${response}=    取得最後API回應
    ${status_code}=    Evaluate    str($response.status_code)
    RETURN    ${status_code}

取得 HTTP Header 值
    [Arguments]    ${header_name}
    IF    $header_name == ''
        Fail    HTTP Header 名稱不可為空白。
    END

    ${response}=    取得最後API回應
    ${actual}=    從回應取得HTTP Header值    ${response}    ${header_name}
    RETURN    ${actual}

檢查 HTTP Header 是否存在
    [Arguments]    ${header_name}
    IF    $header_name == ''
        Fail    HTTP Header 名稱不可為空白。
    END

    ${response}=    取得最後API回應
    ${actual}=    從回應檢查HTTP Header是否存在    ${response}    ${header_name}
    RETURN    ${actual}

取得 JSON 欄位值
    [Arguments]    ${json_path}
    IF    $json_path == ''
        Fail    JSON 路徑不可為空白。
    END

    ${response}=    取得最後API回應
    ${actual}=    從回應取得JSON欄位值    ${response}    ${json_path}
    RETURN    ${actual}

取得 JSON 欄位型別
    [Arguments]    ${json_path}=${EMPTY}
    ${response}=    取得最後API回應
    ${actual}=    從回應取得JSON欄位型別    ${response}    ${json_path}
    RETURN    ${actual}

檢查 JSON 欄位型別加值
    [Arguments]    ${json_path}    ${expected_type}    ${value_condition}
    ${response}=    取得最後API回應
    ${actual}=    從回應檢查JSON欄位型別加值    ${response}    ${json_path}    ${expected_type}    ${value_condition}
    RETURN    ${actual}

檢查 JSON 欄位符合正則
    [Arguments]    ${json_path}    ${pattern}
    ${response}=    取得最後API回應
    ${actual}=    從回應檢查JSON欄位符合正則    ${response}    ${json_path}    ${pattern}
    RETURN    ${actual}

檢查 JSON 欄位日期格式
    [Arguments]    ${json_path}    ${date_format}
    ${response}=    取得最後API回應
    ${actual}=    從回應檢查JSON欄位日期格式    ${response}    ${json_path}    ${date_format}
    RETURN    ${actual}

取得 JSON 陣列長度
    [Arguments]    ${json_path}=${EMPTY}
    ${response}=    取得最後API回應
    ${actual}=    從回應取得JSON陣列長度    ${response}    ${json_path}
    RETURN    ${actual}

檢查 JSON 欄位集合
    [Arguments]    ${expected_fields}=${EMPTY}    ${match_mode}=contains    ${json_path}=${EMPTY}
    ${response}=    取得最後API回應
    ${actual}=    從回應檢查JSON欄位集合    ${response}    ${expected_fields}    ${match_mode}    ${json_path}
    RETURN    ${actual}

檢查 JSON 簡化Schema
    [Arguments]    ${schema_rules}    ${json_path}=${EMPTY}
    ${response}=    取得最後API回應
    ${actual}=    從回應檢查JSON簡化Schema    ${response}    ${schema_rules}    ${json_path}
    RETURN    ${actual}

檢查 JSON 欄位是否存在
    [Arguments]    ${json_path}
    IF    $json_path == ''
        Fail    JSON 路徑不可為空白。
    END

    ${response}=    取得最後API回應
    ${actual}=    從回應檢查JSON欄位是否存在    ${response}    ${json_path}
    RETURN    ${actual}

檢查 JSON 欄位是否為空
    [Arguments]    ${json_path}
    IF    $json_path == ''
        Fail    JSON 路徑不可為空白。
    END

    ${response}=    取得最後API回應
    ${actual}=    從回應檢查JSON欄位是否為空    ${response}    ${json_path}
    RETURN    ${actual}

檢查 JSON 欄位不存在或為空
    [Arguments]    ${json_path}
    IF    $json_path == ''
        Fail    JSON 路徑不可為空白。
    END

    ${response}=    取得最後API回應
    ${actual}=    從回應檢查JSON欄位不存在或為空    ${response}    ${json_path}
    RETURN    ${actual}

依條件取得 JSON 陣列欄位值
    [Arguments]    ${condition_expression}    ${target_path}=${EMPTY}    ${array_path}=${EMPTY}    ${query_options}=${EMPTY}
    IF    $condition_expression == ''
        Fail    條件式不可為空白。
    END

    ${response}=    取得最後API回應
    ${actual}=    從回應依條件取得JSON陣列欄位值    ${response}    ${condition_expression}    ${target_path}    ${array_path}    ${query_options}
    RETURN    ${actual}

依條件取得 JSON 陣列結果數量
    [Arguments]    ${condition_expression}    ${array_path}=${EMPTY}    ${query_options}=${EMPTY}
    IF    $condition_expression == ''
        Fail    條件式不可為空白。
    END

    ${response}=    取得最後API回應
    ${actual}=    從回應依條件取得JSON陣列結果數量    ${response}    ${condition_expression}    ${array_path}    ${query_options}
    RETURN    ${actual}

依條件取得 JSON 陣列所有欄位值
    [Arguments]    ${condition_expression}    ${target_path}=${EMPTY}    ${array_path}=${EMPTY}    ${query_options}=${EMPTY}
    IF    $condition_expression == ''
        Fail    條件式不可為空白。
    END

    ${response}=    取得最後API回應
    ${actual}=    從回應依條件取得JSON陣列所有欄位值    ${response}    ${condition_expression}    ${target_path}    ${array_path}    ${query_options}
    RETURN    ${actual}

依條件檢查 JSON 陣列項目是否存在
    [Arguments]    ${condition_expression}    ${array_path}=${EMPTY}    ${query_options}=${EMPTY}
    IF    $condition_expression == ''
        Fail    條件式不可為空白。
    END

    ${response}=    取得最後API回應
    ${actual}=    從回應依條件檢查JSON陣列項目是否存在    ${response}    ${condition_expression}    ${array_path}    ${query_options}
    RETURN    ${actual}

重置本地JSON資料
    重置本地JSON資料來源

載入本地JSON資料
    [Arguments]    ${file_path}    ${alias}=${EMPTY}
    IF    $file_path == ''
        Fail    JSON 檔案路徑不可為空白。
    END

    ${source_alias}=    Set Variable If    $alias != ''    ${alias}    ${file_path}
    載入本地JSON資料來源    ${source_alias}    ${file_path}
    RETURN    ${source_alias}

取得本地JSON欄位值
    [Arguments]    ${source_alias}    ${json_path}=${EMPTY}    ${array_path}=${EMPTY}    ${condition_expression}=${EMPTY}    ${query_options}=${EMPTY}    ${normalizer}=${EMPTY}
    IF    $source_alias == ''
        Fail    JSON 資料來源別名不可為空白。
    END

    IF    $condition_expression != ''
        ${actual}=    從本地JSON資料來源依條件取得欄位值    ${source_alias}    ${condition_expression}    ${json_path}    ${array_path}    ${query_options}
    ELSE
        ${actual}=    從本地JSON資料來源取得欄位值    ${source_alias}    ${json_path}
    END

    ${normalized_value}=    正規化比對值    ${actual}    ${normalizer}
    RETURN    ${normalized_value}

取得本地JSON結果數量
    [Arguments]    ${source_alias}    ${array_path}=${EMPTY}    ${condition_expression}=${EMPTY}    ${query_options}=${EMPTY}    ${normalizer}=${EMPTY}
    IF    $source_alias == ''
        Fail    JSON 資料來源別名不可為空白。
    END

    ${actual}=    從本地JSON資料來源取得結果數量    ${source_alias}    ${array_path}    ${condition_expression}    ${query_options}
    ${normalized_value}=    正規化比對值    ${actual}    ${normalizer}
    RETURN    ${normalized_value}

使用本地JSON欄位值填入頁面欄位
    [Arguments]    ${selector}    ${source_alias}    ${json_path}=${EMPTY}    ${array_path}=${EMPTY}    ${condition_expression}=${EMPTY}    ${query_options}=${EMPTY}    ${normalizer}=${EMPTY}
    ${value}=    取得本地JSON欄位值    ${source_alias}    ${json_path}    ${array_path}    ${condition_expression}    ${query_options}    ${normalizer}
    Browser.Fill Text    ${selector}    ${value}
    RETURN    ${value}

驗證頁面欄位與JSON欄位一致
    [Arguments]    ${selector}    ${source_alias}    ${json_path}=${EMPTY}    ${array_path}=${EMPTY}    ${condition_expression}=${EMPTY}    ${query_options}=${EMPTY}    ${page_normalizer}=${EMPTY}    ${json_normalizer}=${EMPTY}
    ${page_value}=    Browser.Get Text    ${selector}
    ${page_value}=    正規化比對值    ${page_value}    ${page_normalizer}
    ${json_value}=    取得本地JSON欄位值    ${source_alias}    ${json_path}    ${array_path}    ${condition_expression}    ${query_options}    ${json_normalizer}
    ${failure_message}=    Set Variable    頁面欄位與 JSON 欄位不一致: selector=${selector}, source_alias=${source_alias}, json_path=${json_path}, condition_expression=${condition_expression}, page_value=${page_value}, json_value=${json_value}
    Should Be Equal As Strings    ${page_value}    ${json_value}    msg=${failure_message}
    RETURN    ${page_value}

驗證頁面欄位與JSON筆數一致
    [Arguments]    ${selector}    ${source_alias}    ${array_path}=${EMPTY}    ${condition_expression}=${EMPTY}    ${query_options}=${EMPTY}    ${page_normalizer}=${EMPTY}    ${json_normalizer}=${EMPTY}
    ${page_value}=    Browser.Get Text    ${selector}
    ${page_value}=    正規化比對值    ${page_value}    ${page_normalizer}
    ${json_value}=    取得本地JSON結果數量    ${source_alias}    ${array_path}    ${condition_expression}    ${query_options}    ${json_normalizer}
    ${failure_message}=    Set Variable    頁面欄位與 JSON 筆數不一致: selector=${selector}, source_alias=${source_alias}, array_path=${array_path}, condition_expression=${condition_expression}, page_value=${page_value}, json_value=${json_value}
    Should Be Equal As Strings    ${page_value}    ${json_value}    msg=${failure_message}
    RETURN    ${page_value}

轉換參數值
    [Arguments]    ${value}
    IF    $value == 'True'
        RETURN    ${TRUE}
    ELSE IF    $value == 'False'
        RETURN    ${FALSE}
    ELSE IF    $value == 'None'
        RETURN    ${NONE}
    END
    RETURN    ${value}

驗證步驟結果
    [Arguments]    ${actual}    ${operator}    ${expected}
    IF    '${operator}' == '=='
        Should Be Equal As Strings    ${actual}    ${expected}
    ELSE IF    '${operator}' == '!='
        Should Not Be Equal As Strings    ${actual}    ${expected}
    ELSE IF    '${operator}' == 'contains'
        Should Contain    ${actual}    ${expected}
    ELSE IF    '${operator}' == 'not contains'
        Should Not Contain    ${actual}    ${expected}
    ELSE IF    '${operator}' == '>'
        ${actual_number}=    Convert To Number    ${actual}
        ${expected_number}=    Convert To Number    ${expected}
        Should Be True    $actual_number > $expected_number
    ELSE IF    '${operator}' == '>='
        ${actual_number}=    Convert To Number    ${actual}
        ${expected_number}=    Convert To Number    ${expected}
        Should Be True    $actual_number >= $expected_number
    ELSE IF    '${operator}' == '<'
        ${actual_number}=    Convert To Number    ${actual}
        ${expected_number}=    Convert To Number    ${expected}
        Should Be True    $actual_number < $expected_number
    ELSE IF    '${operator}' == '<='
        ${actual_number}=    Convert To Number    ${actual}
        ${expected_number}=    Convert To Number    ${expected}
        Should Be True    $actual_number <= $expected_number
    ELSE
        Fail    不支援的驗證運算子: ${operator}
    END