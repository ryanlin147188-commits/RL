/**
 * 截圖檢視器
 *
 * 顯示「動作前截圖」與「動作後截圖」。
 * 動作前截圖上方會疊加一個紅色高亮框（Highlight Box），
 * 精準標示自動化程式當時操作的 DOM 元素。
 *
 * @param {{ step: object }} props
 *   step.pre_screenshot_url    - 動作前截圖 URL
 *   step.post_screenshot_url   - 動作後截圖 URL
 *   step.target_highlight_json - 紅框座標，格式：
 *     { top: "35%", left: "25%", width: "50%", height: "10%" }
 */
export default function ScreenshotViewer({ step }) {
  const { pre_screenshot_url, post_screenshot_url, target_highlight_json } = step ?? {}

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
      {/* ── 動作前截圖 ── */}
      <div>
        <p style={{ fontSize: 11, color: '#8b949e', marginBottom: 6, marginTop: 0 }}>
          ▶ 動作前截圖（含目標元素高亮框）
        </p>
        {pre_screenshot_url ? (
          <div style={{ position: 'relative', display: 'inline-block', width: '100%' }}>
            <img
              src={pre_screenshot_url}
              alt="動作前截圖"
              style={{
                width: '100%',
                borderRadius: 6,
                border: '1px solid #30363d',
                display: 'block',
              }}
            />
            {/* 紅色高亮框：絕對定位，座標來自後端 target_highlight_json */}
            {target_highlight_json && (
              <div
                style={{
                  position: 'absolute',
                  top: target_highlight_json.top,
                  left: target_highlight_json.left,
                  width: target_highlight_json.width,
                  height: target_highlight_json.height,
                  border: '2px solid #ef4444',
                  borderRadius: 3,
                  boxShadow:
                    '0 0 0 3px rgba(239, 68, 68, 0.3), 0 0 16px rgba(239, 68, 68, 0.2)',
                  pointerEvents: 'none',
                }}
              />
            )}
          </div>
        ) : (
          <NoScreenshot label="動作前截圖" />
        )}
      </div>

      {/* ── 動作後截圖 ── */}
      <div>
        <p style={{ fontSize: 11, color: '#8b949e', marginBottom: 6, marginTop: 0 }}>
          ◀ 動作後截圖（執行結果驗證）
        </p>
        {post_screenshot_url ? (
          <img
            src={post_screenshot_url}
            alt="動作後截圖"
            style={{
              width: '100%',
              borderRadius: 6,
              border: '1px solid #30363d',
              display: 'block',
            }}
          />
        ) : (
          <NoScreenshot label="動作後截圖" />
        )}
      </div>
    </div>
  )
}

function NoScreenshot({ label }) {
  return (
    <div
      style={{
        height: 160,
        background: '#0d1117',
        border: '1px dashed #30363d',
        borderRadius: 6,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 6,
        color: '#6b7280',
      }}
    >
      <span style={{ fontSize: 24 }}>🖼</span>
      <span style={{ fontSize: 12 }}>無{label}</span>
    </div>
  )
}
