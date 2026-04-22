*** Settings ***
Library    Browser

*** Test Cases ***
T
    ${LOC}=    Set Variable    text=Hello World
    Log    LOC IS ${LOC}
    New Browser    chromium    headless=true
    New Page    about:blank
    Click    ${LOC}
*** Settings ***
Library    Browser

*** Test Cases ***
T
    ${LOC}=    Set Variable    text=Hello World
    Log    LOC IS ${LOC}
    New Browser    chromium    headless=true
    New Page    about:blank
    Click    ${LOC}
