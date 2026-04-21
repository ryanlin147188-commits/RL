*** Settings ***
Documentation    DDT (Data-Driven Testing) 模板資源檔。
...              當 testcase 帶有 ddt_json (n 列資料) 時，後端會在組裝 .robot
...              時把每一列展開為一個獨立的 Test Case 並透過 Test Template
...              呼叫 ``執行DDT資料列``，藉此達到「N×TestCase」的執行語意，
...              讓報表能單獨呈現每一筆資料的 PASS/FAIL。
Resource         core_keywords.resource

*** Keywords ***
執行DDT資料列
    [Documentation]    一列資料對應一個 Test Case；資料以 ``${row_json}`` 字串
    ...                形式傳入 (JSON-encoded dict)，Keyword 內部會將欄位
    ...                注入為 test variable，以便底下的步驟用 ${欄位名} 引用。
    [Arguments]    ${row_json}
    ${row}=    Evaluate    __import__('json').loads($row_json)
    FOR    ${key}    IN    @{row.keys()}
        Set Test Variable    ${${key}}    ${row}[${key}]
    END
    Log    DDT 資料列已注入: ${row}
