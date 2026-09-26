"""视觉抓取引擎：截屏把「屏幕看到的表格」转成表格（T21a GLM / T22 本地库优先）。

DOM 框选（picker）之外的第二条取数路：页面结构读不出语义表格/卡片时
（canvas 渲染、影子 DOM、图片表格），截一张整页 PNG 交识别引擎。两条
引擎：

- local（缺省，T22 sheng 拍板「先不要用大模型，先库来实现」）：离线两级
  识别——img2table+tesseract 先找有边框表格（首行作列名）；找不到再
  rapidocr 全文块版面聚类（x 空隙分栏 + 栏内 y 聚类成行）。不出网。
- glm：截屏交 bigmodel 视觉模型（glm-4v-plus），按固定提示词要求它输出
  {"rows": [...]} 纯 JSON，解析成 list[dict]。纯标准库 urllib 发请求；
  GLM_API_KEY 从环境变量取（不落盘、不进账本）。

错误口径：engine 非法 / glm 缺 key → ValueError（配置问题，人话指引）；
HTTP 非 200 / 响应格式异常 / 模型输出不合规 / 本地 tesseract 失败 →
RuntimeError / ValueError 人话。所有错误信息不回显 key（key 只出现在
Authorization 头里，不进任何消息）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
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


# ----------------------------------------------------------------------
# 本地识别（T22）：img2table 有边框表格优先，rapidocr 版面聚类兜底
# ----------------------------------------------------------------------

_COL_GAP_RATIO = 0.08  # 相邻文本块中心距 > 图宽 8% 切新栏
_Y_CLUSTER_PX = 8      # 栏内按 y 聚类成行的容差（px）
_rapidocr_ocr = None   # RapidOCR 模型加载一次进程内复用（首次数秒）


def screenshot_local(page) -> str:
    """截当前页整页 PNG 落临时文件，返回路径（local 引擎入口）。

    screenshot(type="png") 字节流写 tempfile（.png 后缀，img2table/
    rapidocr 都按路径读盘）。调用方负责删临时文件（screenshot_table
    的 local 路径用完即删）；忘删也只是 /tmp 垃圾，系统会回收。
    """
    png = page.screenshot(type="png")
    fd, path = tempfile.mkstemp(suffix=".png", prefix="pageplay-vision-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(png)
    return path


def extract_local(png_path: str) -> list[dict]:
    """本地两级识别 PNG → rows [{"列": "值"}]（离线，不出网）。

    第一级 img2table：只找有边框表格，取第一个表。第二级（第一级没出
    数据行时）：rapidocr 全文块 + 版面聚类，行 dict 键 "栏1".."栏N"。
    两级都空 → []（"没有"是正常答案，成败口径由调用方定）。
    """
    rows = _extract_bordered_table(png_path)
    if rows:
        return rows
    blocks, img_width = _ocr_text_blocks(png_path)
    return _cluster_text_blocks(blocks, img_width)


def _is_cjk(ch: str) -> bool:
    """常用汉字区（URO U+4E00-U+9FFF）判定，供空格收敛用。"""
    return "一" <= ch <= "鿿"


def _squeeze_cjk_spaces(text: str) -> str:
    """去两侧都是汉字的空格（tesseract 对中文爱插词间空格：订单 号 /
    已 发 货 → 订单号 / 已发货）。英文/数字之间的间距保留（两侧不全
    是 CJK 不动），是 OCR 后处理的通行做法，不是内容改写。
    """
    out: list[str] = []
    for i, ch in enumerate(text):
        if (ch == " " and out and _is_cjk(out[-1])
                and i + 1 < len(text) and _is_cjk(text[i + 1])):
            continue
        out.append(ch)
    return "".join(out)


def _cell_str(value) -> str:
    """df 单元格 → str：NaN/None → ""，其余 str() + CJK 空格收敛。"""
    if value is None or value != value:  # v != v 即 NaN
        return ""
    return _squeeze_cjk_spaces(str(value))


def _extract_bordered_table(png_path: str) -> list[dict]:
    """img2table 找有边框表格：df 首行作列名，其余行为数据。

    实测（img2table 2.0.0 + tesseract 5.5，边框表）：df 首行就是表头
    行、列名轴是整数 0..N-1，故选「首行作列名」而非合成 "列N"——用户
    拿到 订单号/价格 这类真列名。同名表头按 dict(zip) 口径留末列值；
    空单元格（NaN/None）→ ""；表头之外没有数据行 → []（交给第二级）。
    df 在 2.0.0 是属性、旧版是方法，callable 双兼容。tesseract 环境
    问题 → RuntimeError 人话。
    """
    from img2table.document import Image
    from img2table.ocr import TesseractOCR
    doc = Image(src=png_path)
    try:
        tables = doc.extract_tables(ocr=TesseractOCR(lang="chi_sim+eng"))
    except Exception as exc:
        raise RuntimeError(
            f"本地表格识别失败（img2table/tesseract）：{exc}") from None
    if not tables:
        return []
    df = tables[0].df() if callable(tables[0].df) else tables[0].df
    if len(df) < 2:
        return []
    header = [_cell_str(v) or f"列{j + 1}"
              for j, v in enumerate(df.values[0])]
    return [dict(zip(header, (_cell_str(v) for v in row)))
            for row in df.values[1:]]


def _ocr_text_blocks(png_path: str) -> tuple[list[tuple], int]:
    """rapidocr 全文块 → ([(cx, cy, text)], 图宽)（聚类纯函数的输入）。

    box 四角取中心作聚类坐标；识别不出的空结果 → 空块表。图宽经
    cv2 读尺寸（本地库职责之一），读不了 → ValueError 人话。
    """
    global _rapidocr_ocr
    if _rapidocr_ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapidocr_ocr = RapidOCR()
    import cv2
    img = cv2.imread(png_path)
    if img is None:
        raise ValueError(f"读不了截图文件：{png_path}")
    result, _ = _rapidocr_ocr(png_path)
    blocks = []
    for box, text, _score in (result or []):
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        blocks.append((sum(xs) / 4.0, sum(ys) / 4.0,
                       _squeeze_cjk_spaces(str(text))))
    return blocks, img.shape[1]


def _cluster_text_blocks(blocks: list[tuple], img_width: float) -> list[dict]:
    """纯函数：[(cx, cy, text)] → 分栏聚行 → [{"栏1": .., "栏2": ..}]。

    分栏：块按 cx 排序，与前一块中心距 > 图宽×8% 切新栏（栏序即 x 序）。
    聚行：栏内按 cy 排序，距行首块 cy ≤ 8px 并入该行（锚定行首块，不
    链式累积），同行块按 cx 序以空格连接。各栏行数取最大，缺位补 ""。
    """
    if not blocks:
        return []
    ordered = sorted(blocks, key=lambda b: b[0])  # cx 序
    cols: list[list[tuple]] = []
    for blk in ordered:
        if not cols or blk[0] - cols[-1][-1][0] > img_width * _COL_GAP_RATIO:
            cols.append([blk])
        else:
            cols[-1].append(blk)
    col_lines: list[list[str]] = []
    for col in cols:
        col.sort(key=lambda b: b[1])  # 栏内 cy 序
        lines: list[list[tuple]] = []
        for blk in col:
            if lines and blk[1] - lines[-1][0][1] <= _Y_CLUSTER_PX:
                lines[-1].append(blk)
            else:
                lines.append([blk])
        col_lines.append([" ".join(t for _, _, t in
                                   sorted(line, key=lambda b: b[0]))
                          for line in lines])
    n_rows = max(len(lines) for lines in col_lines)
    return [{"栏%d" % (c + 1): (col_lines[c][i]
                                if i < len(col_lines[c]) else "")
             for c in range(len(col_lines))}
            for i in range(n_rows)]


# ----------------------------------------------------------------------
# GLM 视觉（T21a，T22 起降为 engine="glm" 可选引擎）与引擎路由
# ----------------------------------------------------------------------

def _screenshot_table_glm(page, api_key: str | None = None,
                          model: str | None = None) -> list[dict]:
    """截当前页整页 PNG 交 GLM 视觉模型，返回识别出的表格行 [{"列": "值"}]。

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


def screenshot_table(page, engine: str = "local", api_key: str | None = None,
                     model: str | None = None) -> list[dict]:
    """识别引擎路由：local（缺省，离线）/ glm（GLM 视觉，需 key）。

    local → screenshot_local 落临时 PNG + extract_local 两级识别，用完
    即删（识别失败也删）。glm → 既有 GLM 流程原样。其他 engine 值 →
    ValueError 人话点名可选值。返回口径同各引擎：[] = 没识别到。
    """
    if engine == "local":
        png_path = screenshot_local(page)
        try:
            return extract_local(png_path)
        finally:
            try:
                os.remove(png_path)
            except OSError:
                pass
    if engine == "glm":
        return _screenshot_table_glm(page, api_key=api_key, model=model)
    raise ValueError(f"未知的视觉引擎：{engine!r}"
                     "（可选 local=本地 OCR 离线 / glm=GLM 视觉模型）")
