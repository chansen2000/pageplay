"""run 执行账本（纯数据层）：JSONL 追加落账与倒序读取。

pick 确认即执行与 run 确定性重放共用：每次执行（无论成败）经
record_run 追加一行 JSON；results 回看走 load_runs 倒序读最近记录。
账本只追加从不回读旧内容、只加不改删——文件里已有的坏行不影响新记录
写入，读取时单行损坏也只跳过该行，不让一条坏行炸掉整个回看。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("recipe", "action", "status", "detail", "outputs")
_STATUSES = ("ok", "fail")


def record_run(runs_path: Path, entry: dict) -> None:
    """校验并把一条执行记录追加为 runs_path 的一行 JSON（JSONL）。

    - REQUIRED_FIELDS 任一缺失/为 None/空白串 → ValueError（消息点名缺哪些）
    - status 只认 ok / fail，outputs 必须是列表（可为空）→ ValueError
    - created_at 自动补（ISO 秒级）；entry 已带则原样保留
    - 只追加不回读：文件里已有坏行不影响写入；目录不存在逐级创建
    """
    missing = [f for f in REQUIRED_FIELDS
               if f not in entry or entry[f] is None
               or (f != "outputs" and str(entry[f]).strip() == "")]
    if missing:
        raise ValueError(
            f"执行记录缺少必填字段：{', '.join(missing)}"
            f"（必填：{', '.join(REQUIRED_FIELDS)}）")
    if entry["status"] not in _STATUSES:
        raise ValueError(
            f"非法 status：{entry['status']!r}；只支持 {' / '.join(_STATUSES)}")
    if not isinstance(entry["outputs"], list):
        raise ValueError(
            f"outputs 必须是列表（可为空），实际是 "
            f"{type(entry['outputs']).__name__}：{entry['outputs']!r}")

    runs_path = Path(runs_path)
    payload = dict(entry)
    if not payload.get("created_at"):
        payload["created_at"] = datetime.now().isoformat(timespec="seconds")
    runs_path.parent.mkdir(parents=True, exist_ok=True)
    with runs_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    log.info("record_run: %s %s %s -> %s", payload["recipe"],
             payload["action"], payload["status"], runs_path)


def load_runs(runs_path: Path, recipe: str | None = None,
              limit: int = 20) -> list[dict]:
    """读执行记录：新在前（倒序）最多 limit 条；recipe 给定则只看该 recipe。

    文件不存在 → 空列表；limit <= 0 → 空列表；单行损坏/非 dict →
    log.warning 跳过该行（坏行只可能是外力所致，账本自身只追加），
    不让一条坏行炸掉整个回看。
    """
    runs_path = Path(runs_path)
    if limit <= 0 or not runs_path.is_file():
        return []
    entries: list[dict] = []
    text = runs_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            log.warning("执行记录行损坏，已跳过：%.80s", line)
            continue
        if not isinstance(data, dict):
            log.warning("执行记录行不是对象，已跳过：%.80s", line)
            continue
        if recipe is not None and data.get("recipe") != recipe:
            continue
        entries.append(data)
    return entries[-limit:][::-1]
