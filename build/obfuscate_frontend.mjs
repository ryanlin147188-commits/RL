#!/usr/bin/env node
/**
 * 前端 JavaScript 重度混淆。
 *
 * 從 frontend/index.html 抓出所有 <script>...</script> 內容,跑
 * javascript-obfuscator,把混淆結果替回原位,輸出 index.production.html。
 *
 * 跑法(在 build/Dockerfile.frontend.production 的 Stage 1 內):
 *     node obfuscate_frontend.mjs <input.html> <output.html>
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { argv, exit } from 'node:process';
import JsObfuscator from 'javascript-obfuscator';

const inputPath  = argv[2] || 'index.html';
const outputPath = argv[3] || 'index.production.html';

console.log(`[obfuscate] in=${inputPath} out=${outputPath}`);

let html = readFileSync(inputPath, 'utf8');

// 設定:中重度混淆(stringArray + controlFlowFlattening + identifier hex)
// 不開 selfDefending / debugProtection — 嚴重影響效能 + 客戶 debug 麻煩
const opts = {
    compact: true,
    controlFlowFlattening: true,
    controlFlowFlatteningThreshold: 0.7,
    deadCodeInjection: true,
    deadCodeInjectionThreshold: 0.3,
    identifierNamesGenerator: 'hexadecimal',
    numbersToExpressions: true,
    simplify: true,
    splitStrings: true,
    splitStringsChunkLength: 8,
    stringArray: true,
    stringArrayEncoding: ['base64'],
    stringArrayThreshold: 0.75,
    transformObjectKeys: true,
    unicodeEscapeSequence: false,
    // 保留下列保留字 / 既有全域(若被混淆會破壞 onclick="xxx" inline handler):
    reservedNames: [
        // 大量被 HTML inline `onclick=` / `onchange=` 引用的全域函式
        // 必須保留原名,否則 HTML inline handler 找不到 function
        '^show[A-Z].*View$',          // showHomeView, showDefectsView, ...
        '^open[A-Z].*Modal$',         // openTodoModal, openDefectModal, ...
        '^close[A-Z].*Modal$',
        '^render[A-Z].*$',            // renderXxx
        '^reload[A-Z].*$',
        '^todoSubmit$', '^defectSubmit$', '^milestoneSubmit$', '^planSubmit$',
        '^todoQuickToggle$', '^deleteTodo$', '^deleteDefect$',
        '^addTodoLink$', '^removeTodoLink$', '^onTodoLinkTypeChange$',
        '^showLinkedTodosPopover$', '^showBacklogView$',
        '^toggleBacklogAddMenu$', '^closeBacklogAddMenu$',
        '^kanbanCardClick$', '^kanbanDrag.*$', '^kanbanDrop$',
        '^selectMock$', '^selectDbConfig$',
        '^updateSelectedMockField$', '^updateSelectedDbConfigField$',
        '^addMockEndpoint$', '^addDbConfig$',
        '^saveMockEndpoints$', '^saveDbConfigs$',
        '^removeSelectedMock$', '^removeSelectedDbConfig$',
        '^duplicateSelectedMock$', '^duplicateSelectedDbConfig$',
        '^testSelectedDbConfig$', '^runDbConfigQuery$',
        '^togglePwdVisibility$',
        '^fillMockBodyTemplate$', '^formatMockBodyJson$',
        '^fillMockReqBodyTemplate$', '^formatMockReqBodyJson$',
        '^onTodoTypeChange$',
        '^reloadHomeTodos$', '^reloadBacklogView$',
        '^_setView.*$', '^_hideAllWorkspaces$',
        '^rtm.*$',                    // rtmSwitchTab、rtmOpenDefect、rtmChainToggle 等
        '^openRequirementModal$', '^deleteRequirement$',
        '^closeMobileSidebar$', '^switchProject$',
        '^openNodeModal$', '^renameCurrentProject$', '^deleteCurrentProject$',
        '^_setViewMeta$', '^_afterViewChange$',
        '^confirmDiscardIfDirty$',
    ],
    reservedStrings: [],
    // 保留 i18n key 字串,否則查不到字典(I18N_DICT 用字串 key 索引)
    forceTransformStrings: [],
};

// 抽出所有 <script> ... </script>(不含 src 的 inline script);只混淆 inline。
// 注意:用簡單 regex,因為這個 HTML 沒有複雜的 nested script 注入。
const scriptRe = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;

let count = 0, totalIn = 0, totalOut = 0;
const newHtml = html.replace(scriptRe, (full, attrs, body) => {
    if (/\bsrc\s*=/.test(attrs)) {
        // 有 src 屬性 = 外部 script(Tailwind / Chart.js / FontAwesome CDN),不動
        return full;
    }
    if (!body.trim()) return full;
    count += 1;
    totalIn += body.length;
    const obf = JsObfuscator.obfuscate(body, opts).getObfuscatedCode();
    totalOut += obf.length;
    return `<script${attrs}>${obf}</script>`;
});

writeFileSync(outputPath, newHtml, 'utf8');
console.log(`[obfuscate] processed ${count} <script> blocks`);
console.log(`[obfuscate] in=${(totalIn/1024).toFixed(0)} KB → out=${(totalOut/1024).toFixed(0)} KB (×${(totalOut/totalIn).toFixed(2)})`);
console.log(`[obfuscate] HTML in=${(html.length/1024).toFixed(0)} KB → out=${(newHtml.length/1024).toFixed(0)} KB`);
