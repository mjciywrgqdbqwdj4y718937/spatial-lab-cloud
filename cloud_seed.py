# -*- coding: utf-8 -*-
"""云端平台种子：把静态站的精选模型映射成平台 work 目录结构。

在服务器上运行（容器/主机均可）：
  cd /opt/lab/spatial-lab-library && python3 cloud_seed.py
"""
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data.json")
WORK = os.path.join(os.path.dirname(BASE), "spatial-lab-cloud", "work")


def main():
    if not os.path.exists(DATA):
        print("缺少 data.json")
        return 1
    d = json.load(open(DATA, encoding="utf-8"))
    models = d.get("models", [])
    os.makedirs(WORK, exist_ok=True)

    made = skipped = 0
    for m in models:
        glb_rel = m.get("model")
        if not glb_rel:
            continue  # 没有可下载模型，只展示
        jid = m["id"]
        src_glb = os.path.join(BASE, glb_rel)
        if not os.path.exists(src_glb):
            skipped += 1
            continue
        dst_dir = os.path.join(WORK, jid)
        os.makedirs(dst_dir, exist_ok=True)
        dst_glb = os.path.join(dst_dir, "model.glb")
        if not os.path.exists(dst_glb):
            shutil.copyfile(src_glb, dst_glb)
        thumb_rel = m.get("thumb")
        if thumb_rel:
            src_thumb = os.path.join(BASE, thumb_rel)
            if os.path.exists(src_thumb):
                ext = os.path.splitext(src_thumb)[1]
                dst_thumb = os.path.join(dst_dir, "thumb" + ext)
                if not os.path.exists(dst_thumb):
                    shutil.copyfile(src_thumb, dst_thumb)
        jobf = os.path.join(dst_dir, "job.json")
        if not os.path.exists(jobf):
            job = {
                "job_id": jid, "pipeline": "library", "pipeline_name": "云端精选",
                "status": "done", "progress": 100, "stage": "完成",
                "params": {"source": "GitHub 素材库"},
                "result": {"glb": "model.glb", "tris": m.get("tris", 0),
                           "kb": m.get("kb", 0)},
                "asset_name": m.get("name"),
            }
            with open(jobf, "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False, indent=1)
        made += 1

    print(f"种子完成：可操作模型 {made} 个，缺失 {skipped} 个")
    print("SEED_DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
