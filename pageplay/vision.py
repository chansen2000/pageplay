"""视觉抓取引擎（T21a）：截屏交视觉模型，把「屏幕看到的表格」转成表格。

DOM 框选（picker）之外的第二条取数路：页面结构读不出语义表格/卡片时
（canvas 渲染、影子 DOM、图片表格），截一张整页 PNG 交给 bigmodel 视觉
模型（glm-4v-plus），按固定提示词要求它输出 {"rows": [...]} 纯 JSON，
解析成 list[dict] 交 save_table 落 CSV。纯标准库 urllib 发请求，不新增
第三方依赖；GLM_API_KEY 从环境变量取（不落盘、不进账本）。

错误口径：缺 key → ValueError（配置问题，人话指引）；HTTP 非 200 /
响应格式异常 / 模型输出不合规 → RuntimeError / ValueError 人话。所有
错误信息不回显 key（key 只出现在 Authorization 头里，不进任何消息）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request

VISION_MODEL_DEFAULT = "glm-4v-plus"
ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
PROMPT = ("识别截图中所有表格。输出 JSON：{\"rows\": [{列名: 值}, ...]}，"
          "列名用表格表头；若页面有多张表格合并输出并在列名前标注来源；"
          "没有表格输出 {\"rows\": []}。只输出 JSON，不要其他文字。")
_TIMEOUT_SEC = 120  # 截图上传 + 模型推理，留足慢网余量

# 模型输出常带 ```json 围栏（提示词禁了也拦不住），剥掉再解析
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_vision_json(text: str) -> list[dict]:
    """纯函数：剥 ```json 围栏 → json.loads → 取 ["rows"] 且为 list[dict]。

    围栏可有可无（裸 JSON 直接过）；剥完不是合法 JSON / 顶层不是对象 /
    缺 rows / rows 不是列表 / 行不是对象 → ValueError 人话（都能凭输出
    原文定位是模型没守约定，不是代码错）。
    """
    text = str(text or "").strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(
            "视觉模型没有按要求输出 JSON（剥掉 ```json 围栏后仍解析失败），"
            f"实际开头：{text[:60]!r}；请重试一次") from None
    if not isinstance(data, dict):
        raise ValueError(
            f"视觉模型输出不是 JSON 对象（是 {type(data).__name__}），"
            "取不到 rows；请重试一次")
    rows = data.get("rows")
    if rows is None:
        raise ValueError(
            "视觉模型输出的 JSON 里没有 rows 字段（约定 {\"rows\": […]}）；"
            "请重试一次")
    if not isinstance(rows, list):
        raise ValueError(
            f"视觉模型输出的 rows 不是列表（是 {type(rows).__name__}）；"
            "请重试一次")
    bad = next((r for r in rows if not isinstance(r, dict)), None)
    if bad is not None:
        raise ValueError(
            f"视觉模型输出的 rows 里有不是对象的行：{bad!r}；请重试一次")
    return rows


def _post(payload: dict, api_key: str) -> str:
    """POST ENDPOINT 取模型回复文本（urllib 收敛点，测试在此打桩）。

    返回 choices[0].message.content；HTTP 非 200 / 连不上 / 响应缺结构
    → RuntimeError 人话。错误消息不含请求体与 key（key 只在请求头）。
    """
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"视觉服务返回 HTTP {exc.code}：模型调用失败"
            "（检查 GLM_API_KEY 是否有效、账户额度是否用尽）") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"连不上视觉服务（{ENDPOINT}）：{reason}"
                           "；请检查网络后重试") from None
    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError(
            "视觉服务响应格式异常（choices/message/content 缺失），请重试"
        ) from None
    if not isinstance(content, str):
        raise RuntimeError(
            "视觉服务响应的 content 不是文本，无法解析；请重试")
    return content


def screenshot_table(page, api_key: str | None = None,
                     model: str | None = None) -> list[dict]:
    """截当前页整页 PNG 交视觉模型，返回识别出的表格行 [{"列": "值"}]。

    key = api_key or GLM_API_KEY 环境变量，空 → ValueError（配置指引）。
    模型没识别到表格返回 []（不报错——"没有"是正常答案，由调用方定
    成败口径）。网络/服务/解析异常按 _post / parse_vision_json 口径
    向上抛，由 CLI 顶层统一映射人话与退出码。
    """
    key = api_key or os.environ.get("GLM_API_KEY")
    if not key:
        raise ValueError("视觉抓取需要 GLM_API_KEY 环境变量（bigmodel 密钥），"
                         "设置后重试")
    png = page.screenshot(type="png")
    b64 = base64.b64encode(png).decode("ascii")
    payload = {
        "model": model or VISION_MODEL_DEFAULT,
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": PROMPT},
        ]}],
        "temperature": 0.1,
    }
    content = _post(payload, key)
    return parse_vision_json(content)
