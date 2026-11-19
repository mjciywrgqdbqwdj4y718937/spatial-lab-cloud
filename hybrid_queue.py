# -*- coding: utf-8 -*-
"""混合云队列：把云端提交的 GPU 任务排队，由本地 4060 代跑。

云端 API（挂在 server2.py 里）：
  POST /api/queue/submit   提交任务（pipeline, params, image）
  GET  /api/queue/pending  本地 worker 领取任务（需 token）
  POST /api/queue/result   本地 worker 回传结果（需 token）
  GET  /api/queue/status/{task_id}   前端查询

本文件提供可复用的实现，供 server2.py 引入。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path

# 队列根目录（由 server2.py 注入）
QUEUE_DIR: Path | None = None
QUEUE_TOKEN = os.environ.get("QUEUE_TOKEN", "").strip()


def init(root: Path, token: str = "") -> None:
    global QUEUE_DIR, QUEUE_TOKEN
    QUEUE_DIR = root / "_queue"
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    if token:
        QUEUE_TOKEN = token
    if not QUEUE_TOKEN:
        # 没配 token 时生成一个并写文件，方便本地 worker 读取
        QUEUE_TOKEN = secrets.token_urlsafe(16)
        (root / "queue_token.txt").write_text(QUEUE_TOKEN, encoding="utf-8")


def _task_dir(task_id: str) -> Path:
    assert QUEUE_DIR is not None
    return QUEUE_DIR / task_id


def submit(pipeline: str, params: dict, image_bytes: bytes | None,
           ext: str = ".png", note: str = "") -> str:
    assert QUEUE_DIR is not None
    tid = uuid.uuid4().hex[:12]
    d = _task_dir(tid)
    d.mkdir(parents=True, exist_ok=True)
    if image_bytes:
        (d / ("input" + ext)).write_bytes(image_bytes)
    task = {
        "task_id": tid, "pipeline": pipeline, "params": params,
        "input": ("input" + ext) if image_bytes else None,
        "status": "pending", "created": time.time(),
        "claimed_by": None, "claimed_at": None,
        "result": None, "error": None, "note": note,
    }
    (d / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=1), encoding="utf-8")
    return tid


def load(task_id: str) -> dict | None:
    p = _task_dir(task_id) / "task.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(task: dict) -> None:
    d = _task_dir(task["task_id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=1), encoding="utf-8")


def claim_next(worker_id: str) -> dict | None:
    """领取最早的 pending 任务。"""
    assert QUEUE_DIR is not None
    items = []
    for d in QUEUE_DIR.iterdir():
        if not d.is_dir():
            continue
        t = load(d.name)
        if t and t.get("status") == "pending":
            items.append(t)
    if not items:
        return None
    items.sort(key=lambda x: x.get("created", 0))
    t = items[0]
    t["status"] = "claimed"
    t["claimed_by"] = worker_id
    t["claimed_at"] = time.time()
    save(t)
    return t


def finish(task_id: str, result: dict | None = None, error: str | None = None,
           files: dict[str, bytes] | None = None) -> bool:
    t = load(task_id)
    if not t:
        return False
    d = _task_dir(task_id)
    if files:
        for name, data in files.items():
            (d / name).write_bytes(data)
    t["status"] = "done" if error is None else "failed"
    t["result"] = result
    t["error"] = error
    t["finished"] = time.time()
    save(t)
    return True


def stats() -> dict:
    assert QUEUE_DIR is not None
    counts = {"pending": 0, "claimed": 0, "done": 0, "failed": 0}
    for d in QUEUE_DIR.iterdir():
        if not d.is_dir():
            continue
        t = load(d.name)
        if not t:
            continue
        s = t.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1
    return counts
