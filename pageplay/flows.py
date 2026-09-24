"""flow 纯数据层：字段校验、按名存取、清单、递增命名。record 保存与 run 执行共用。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

REQUIRED = ("version", "name", "site", "url", "steps")
VALID_KINDS = ("goto", "click", "table", "download")
_LIST_KEYS = ("name", "url", "step_count", "created_at")
_FLOWS_DIRNAME = "flows"


def _flow_path(site_dir: Path, name: str) -> Path:
    """flow 文件路径 <site_dir>/flows/<name>.json；名字先过安全校验。

    flow 名直接拼进文件路径，不校验就能用 "../x" 或 "..\\x" 写穿到
    flows 目录外，所以拒收：空名、"."、含 "/" 或 "\\" 或 ".." 的名字。
    """
    name = str(name)
    if (not name.strip() or name == "."
            or "/" in name or "\\" in name or ".." in name):
        raise ValueError(
            f"非法 flow 名：{name!r}；"
            f"名字不能为空、不能是 . ，也不能含路径分隔符或 ..")
    return site_dir / _FLOWS_DIRNAME / f"{name}.json"


def save_flow(site_dir: Path, flow: dict) -> Path:
    """校验并把 flow 落成 <site_dir>/flows/<name>.json，返回文件路径。

    - REQUIRED 任一缺失/为 None/空白串 → ValueError（消息点名缺哪些）
    - steps 必须是非空 list，每个 step 的 kind ∈ VALID_KINDS，否则 ValueError
    - 每个 step 自动补 "no"（1 起连续序号，已带的旧值丢弃重排）；
      写入的是副本，不改调用方传入的 flow 底稿
    - 写入自动带 created_at（ISO 秒级）；flow 已带则原样保留
    - 同名 flow 直接覆盖（log.info 一句），不做多版本并存
    """
    missing = [f for f in REQUIRED
               if f not in flow or flow[f] is None
               or str(flow[f]).strip() == ""]
    if missing:
        raise ValueError(
            f"flow 缺少必填字段：{', '.join(missing)}"
            f"（必填：{', '.join(REQUIRED)}）")

    steps = flow["steps"]
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"flow 的 steps 必须是非空 list，收到：{steps!r}")
    for no, step in enumerate(steps, 1):
        kind = step.get("kind") if isinstance(step, dict) else None
        if kind not in VALID_KINDS:
            raise ValueError(
                f"flow 第 {no} 步的 kind 非法：{kind!r}；"
                f"只支持 {' / '.join(VALID_KINDS)}")

    path = _flow_path(site_dir, flow["name"])
    payload = dict(flow)
    payload["steps"] = [dict(step, no=no) for no, step in enumerate(steps, 1)]
    if not payload.get("created_at"):
        payload["created_at"] = datetime.now().isoformat(timespec="seconds")

    existed = path.is_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    if existed:
        log.info("flow %s 已存在，覆盖写入 %s", flow["name"], path)
    return path


def load_flow(site_dir: Path, name: str) -> dict:
    """按名读 flow，返回 JSON 全量 dict。

    文件缺失 → FileNotFoundError，消息带"先 pageplay record"指引
    和现有 flow 清单，方便挑名字或发现还没保存过。
    """
    path = _flow_path(site_dir, name)
    if not path.is_file():
        names = [f["name"] for f in list_flows(site_dir)]
        known = "、".join(str(n) for n in names) if names else "（一个都没有）"
        raise FileNotFoundError(
            f"flow 不存在：{path}\n"
            f"先 pageplay record 保存一个 flow 再执行；现有 flow：{known}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_flows(site_dir: Path) -> list[dict]:
    """列站点下全部 flow：按 name 排序，每条只留 name/url/step_count/created_at。

    step_count 按 steps 实际条数现算，不信任文件里存的旧值。
    flows 目录不存在 → 空列表；单个文件损坏/非 dict → log.warning
    跳过，不让一份坏文件炸掉整个清单。
    """
    flows_dir = site_dir / _FLOWS_DIRNAME
    if not flows_dir.is_dir():
        return []
    out: list[dict] = []
    for path in flows_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("flow 文件损坏，已跳过：%s", path)
            continue
        if not isinstance(data, dict):
            log.warning("flow 文件内容不是对象，已跳过：%s", path)
            continue
        item = {k: data.get(k) for k in _LIST_KEYS}
        item["step_count"] = len(data.get("steps") or [])  # 覆盖占位，现算
        out.append(item)
    out.sort(key=lambda f: str(f["name"]))
    return out


def next_flow_name(site_dir: Path, site: str) -> str:
    """给录制流程起名：<site>-flow-<N>，N 取现有最大 + 1，没有则 1。

    只认本站点前缀 + 纯数字尾巴（sycm-flow-3）；别的站点前缀、
    非数字尾巴（sycm-flow-abc）都不计入，避免撞名覆盖。
    """
    pattern = re.compile(rf"^{re.escape(str(site))}-flow-(\d+)$")
    max_no = 0
    for item in list_flows(site_dir):
        matched = pattern.match(str(item["name"]))
        if matched:
            max_no = max(max_no, int(matched.group(1)))
    return f"{site}-flow-{max_no + 1}"
