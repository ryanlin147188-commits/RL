"""Robot Framework Library：Screenshot diff 比對。

匯入方式（在生成的 .robot 內）::

    Library    tasks.assert_screenshot_lib    WITH NAME    AssertScreenshot

提供關鍵字 ``Match``，呼叫格式::

    AssertScreenshot.Match    <current_screenshot_path>    <step_uuid>    <threshold_pct>

行為：
  1. 從 MinIO 取得 ``baselines/<step_uuid>.png``
  2. 不存在 → 把 current 上傳當 baseline，PASS（auto-save 模式）
  3. 存在    → 用 Pillow + numpy 計算像素差異 %
       - <= threshold 時 PASS
       - >  threshold 時 FAIL；同時生成「紅色覆蓋」差異圖並上傳

不論 PASS / FAIL，都會 ``Log`` 一個 marker 給 listener 解析::

    SCREENSHOT_DIFF step_uuid=<uuid> baseline=<url> actual=<url> diff=<url> pct=<float>

listener 解析後把 URL 寫入該 step buffer，最終存進 ExecutionStepLog 的
``screenshot_baseline_url`` / ``screenshot_diff_url`` / ``screenshot_diff_pct``。
"""
from __future__ import annotations

import io
import os
import uuid
from typing import Optional

import numpy as np
from PIL import Image, ImageChops
from robot.api import logger
from robot.api.deco import keyword


ROBOT_LIBRARY_SCOPE = "GLOBAL"


def _minio_client():
    import boto3  # type: ignore
    return boto3.client(
        "s3",
        endpoint_url=os.environ["MINIO_ENDPOINT"],
        aws_access_key_id=os.environ["MINIO_ACCESS_KEY"],
        aws_secret_access_key=os.environ["MINIO_SECRET_KEY"],
        region_name="us-east-1",
    )


def _to_url(key: str) -> str:
    """以 / 起頭的相對 URL；前端會用 window.location.origin 拼出絕對 URL。"""
    return f"/results/{key}"


def _download_baseline(step_uuid: str) -> Optional[bytes]:
    s3 = _minio_client()
    key = f"baselines/{step_uuid}.png"
    try:
        obj = s3.get_object(Bucket="results", Key=key)
        return obj["Body"].read()
    except Exception:
        return None


def _upload_bytes(data: bytes, key: str, content_type: str = "image/png") -> str:
    s3 = _minio_client()
    s3.put_object(Bucket="results", Key=key, Body=data, ContentType=content_type)
    return _to_url(key)


def _compute_diff(baseline_png: bytes, current_png: bytes) -> tuple[float, Image.Image]:
    """回傳 (diff_pct, overlay_image)；overlay 為紅色標出差異像素的圖。"""
    bl_img = Image.open(io.BytesIO(baseline_png)).convert("RGB")
    cur_img = Image.open(io.BytesIO(current_png)).convert("RGB")

    # 尺寸不同就把 baseline resize 到 current 大小（避免 ResizeError 直接擋住）
    if bl_img.size != cur_img.size:
        bl_img = bl_img.resize(cur_img.size)

    bl_arr = np.asarray(bl_img, dtype=np.int16)
    cur_arr = np.asarray(cur_img, dtype=np.int16)

    # 每像素 RGB 距離 > 30 視為「真的有差」（避開 JPEG 雜訊 / 抗鋸齒微小差異）
    diff = np.abs(bl_arr - cur_arr).max(axis=2)
    differ_mask = diff > 30

    total = differ_mask.size
    n_diff = int(differ_mask.sum())
    pct = (n_diff / total * 100.0) if total > 0 else 0.0

    # 生成「紅色覆蓋」overlay：current 為底，differ 像素以 60% 不透明紅色覆蓋
    overlay = cur_arr.astype(np.uint8).copy()
    red = np.array([255, 0, 0], dtype=np.uint8)
    alpha = 0.6
    overlay[differ_mask] = (overlay[differ_mask] * (1 - alpha) + red * alpha).astype(np.uint8)
    overlay_img = Image.fromarray(overlay, "RGB")
    return pct, overlay_img


@keyword("Match")
def match(current_screenshot_path: str, step_uuid: str, threshold_pct: float = 1.0) -> None:
    """比對截圖；diff% > threshold 時 raise AssertionError。"""
    if not os.path.isfile(current_screenshot_path):
        raise AssertionError(f"AssertScreenshot.Match: current 截圖不存在 {current_screenshot_path}")

    threshold_pct = float(threshold_pct or 1.0)
    report_id = os.environ.get("AUTOTEST_REPORT_ID", "unknown")

    with open(current_screenshot_path, "rb") as fh:
        current_bytes = fh.read()
    # 上傳當下截圖，作為「actual」供 report 顯示
    actual_key = f"diffs/{report_id}/{step_uuid}_actual_{uuid.uuid4().hex[:6]}.png"
    actual_url = _upload_bytes(current_bytes, actual_key)

    baseline_bytes = _download_baseline(step_uuid)

    if baseline_bytes is None:
        # baseline 不存在 → auto-save current 當 baseline
        baseline_url = _upload_bytes(current_bytes, f"baselines/{step_uuid}.png")
        logger.info(
            f"[AssertScreenshot] baseline 不存在；已自動將當下截圖存為 baseline "
            f"(step_uuid={step_uuid})"
        )
        # marker：listener 解析後寫進 DB
        logger.info(
            f"SCREENSHOT_DIFF step_uuid={step_uuid} baseline={baseline_url} "
            f"actual={actual_url} diff= pct=0.0 status=AUTO_SAVED"
        )
        return  # PASS

    # 有 baseline → diff
    pct, overlay_img = _compute_diff(baseline_bytes, current_bytes)
    baseline_url = _to_url(f"baselines/{step_uuid}.png")  # 已存在的 baseline URL

    diff_url = ""
    if pct > threshold_pct:
        # 差太多才存 diff 圖（節省空間）
        buf = io.BytesIO()
        overlay_img.save(buf, format="PNG")
        diff_url = _upload_bytes(
            buf.getvalue(),
            f"diffs/{report_id}/{step_uuid}_diff_{uuid.uuid4().hex[:6]}.png",
        )

    # marker
    logger.info(
        f"SCREENSHOT_DIFF step_uuid={step_uuid} baseline={baseline_url} "
        f"actual={actual_url} diff={diff_url} pct={pct:.4f} threshold={threshold_pct:.4f}"
    )

    if pct > threshold_pct:
        raise AssertionError(
            f"Screenshot diff {pct:.2f}% 超過容忍門檻 {threshold_pct:.2f}% "
            f"(baseline={baseline_url}, actual={actual_url}, diff={diff_url})"
        )
    logger.info(
        f"[AssertScreenshot] matched diff={pct:.4f}% threshold={threshold_pct:.2f}%"
    )
