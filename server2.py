# -*- coding: utf-8 -*-
"""Wildfire · 3D 生产平台（多管线）

后端能力：
  depth     单图 → 深度 → 点云 → 泊松网格
  triposr   单图 → TripoSR → 带顶点色网格
  simplify  网格简化（二次误差）
  smooth    平滑（Taubin）
  repair    去重 / 去退化 / 补洞 / 法线统一
  convert   GLB / GLTF / OBJ / PLY / STL / OFF / 3MF 互转
  render    软件光栅渲染出图（无 GL 依赖）
  measure   几何统计（面数、顶点、包围盒、水密性、体积）

运行：
  python server.py [端口]        默认 8791
  python server.py 8791 --no-browser
"""
from __future__ import annotations

import io
import json
import math
import multiprocessing as mp
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import List, Optional

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# Windows 沙箱下系统临时目录可能不可写：numba/pymatting 建临时文件会卡死，
# 统一把 TEMP/TMP 与 numba 缓存指到工作区内。
_TMPDIR = Path(__file__).resolve().parent / "tmp"
_TMPDIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TEMP", str(_TMPDIR))
os.environ.setdefault("TMP", str(_TMPDIR))
os.environ.setdefault("NUMBA_CACHE_DIR", str(_TMPDIR.parent / "numba-cache"))
os.environ.setdefault("HF_HOME", r"D:\DSH视频\models\hf-cache")
os.environ.setdefault("DINO_VIT_B16_CONFIG", r"D:\DSH视频\models\TripoSR\dino-vitb16-config.json")

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
UPLOADS = ROOT / "uploads"
STATIC = ROOT / "static"
for d in (WORK, UPLOADS, STATIC, STATIC / "vendor"):
    d.mkdir(parents=True, exist_ok=True)

TRIPOSR_DIR = Path(r"D:\DSH视频\TripoSR")
TRIPOSR_CFG = TRIPOSR_DIR / "model_local" / "config.yaml"
TRIPOSR_CANDIDATES = [
    Path(r"D:\DSH视频\models\TripoSR\model_fixed.ckpt"),
    TRIPOSR_DIR / "model_local" / "model.ckpt",
]

PORT = 8791
OPEN_BROWSER = True
for a in sys.argv[1:]:
    if a.isdigit():
        PORT = int(a)
    elif a == "--no-browser":
        OPEN_BROWSER = False

app = FastAPI(title="Wildfire", version="2.0")
# 公网隧道下 589 KB 的 three.js 是首屏杀手：gzip 后能压到 150 KB 左右
app.add_middleware(GZipMiddleware, minimum_size=600)
BOOT_TAG = str(int(time.time()))
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
ENGINE = {"proc": None, "q_in": None, "q_out": None, "gpu": None, "ready": False,
          "started_at": None, "tasks": 0}
PIPE_LOG: list[dict] = []

# ─── 混合云队列：云端排队、本地 GPU 代跑 ───
try:
    import hybrid_queue as HQ
    HQ.init(WORK)
except Exception as _e:  # noqa: BLE001
    HQ = None
    print("hybrid_queue 初始化失败:", _e)



def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ts_short() -> str:
    return time.strftime("%H:%M:%S")


# ─────────────────────────── 管线定义（前端自动生成表单） ───────────────────────────
PIPELINES = [
    {
        "id": "depth", "name": "单图重建 · 深度", "group": "重建",
        "desc": "深度估计 → 反投影点云 → 泊松重建 → 平滑简化，输出带顶点色的网格。",
        "needs": "image", "tag": "本地",
        "params": [
            {"k": "faces", "label": "目标面数", "type": "range", "min": 2000, "max": 200000, "step": 1000, "default": 25000},
            {"k": "smooth", "label": "平滑迭代", "type": "range", "min": 0, "max": 40, "step": 1, "default": 18},
            {"k": "fov", "label": "视野角 °", "type": "range", "min": 20, "max": 100, "step": 1, "default": 50},
            {"k": "near", "label": "近平面 m", "type": "range", "min": 0.1, "max": 3, "step": 0.1, "default": 0.5},
            {"k": "far", "label": "远平面 m", "type": "range", "min": 2, "max": 30, "step": 0.5, "default": 8},
        ],
    },
    {
        "id": "triposr", "name": "AI 生成 · TripoSR", "group": "重建",
        "desc": "TripoSR 前馈重建：单张图 → 三平面神经场 → marching cubes → 带顶点色网格。",
        "needs": "image", "tag": "模型",
        "params": [
            {"k": "resolution", "label": "网格分辨率", "type": "select", "options": [128, 192, 256, 320], "default": 256},
            {"k": "threshold", "label": "密度阈值", "type": "range", "min": 10, "max": 60, "step": 1, "default": 25},
            {"k": "vertex_color", "label": "烘焙顶点色", "type": "switch", "default": True},
            {"k": "rembg", "label": "先抠背景", "type": "switch", "default": False},
        ],
    },
    {
        "id": "simplify", "name": "简化", "group": "处理",
        "desc": "二次误差边折叠，把面数压到目标值，保留形体。",
        "needs": "model",
        "params": [
            {"k": "faces", "label": "目标面数", "type": "range", "min": 500, "max": 200000, "step": 500, "default": 20000},
            {"k": "smooth_after", "label": "简化后再平滑", "type": "range", "min": 0, "max": 20, "step": 1, "default": 3},
        ],
    },
    {
        "id": "smooth", "name": "平滑", "group": "处理",
        "desc": "Taubin 平滑，λ/μ 交替抑制体积收缩。",
        "needs": "model",
        "params": [
            {"k": "iterations", "label": "迭代次数", "type": "range", "min": 1, "max": 60, "step": 1, "default": 8},
            {"k": "lambda", "label": "λ 系数", "type": "range", "min": 0.1, "max": 0.9, "step": 0.05, "default": 0.5},
        ],
    },
    {
        "id": "repair", "name": "修复", "group": "处理",
        "desc": "合并重复顶点、去退化三角形、统一法线、补洞，输出水密网格。",
        "needs": "model",
        "params": [
            {"k": "fill_holes", "label": "补洞", "type": "switch", "default": True},
            {"k": "unify_normals", "label": "统一法线", "type": "switch", "default": True},
            {"k": "remove_small", "label": "移除小碎块（面）", "type": "range", "min": 0, "max": 5000, "step": 100, "default": 300},
        ],
    },
    {
        "id": "convert", "name": "格式转换", "group": "处理",
        "desc": "在 GLB / GLTF / OBJ / PLY / STL / OFF / 3MF 之间互转。",
        "needs": "model",
        "params": [
            {"k": "fmt", "label": "目标格式", "type": "select",
             "options": ["glb", "obj", "ply", "stl", "off", "gltf", "3mf"], "default": "obj"},
            {"k": "binary", "label": "PLY 二进制", "type": "switch", "default": False},
        ],
    },
    {
        "id": "render", "name": "渲染出图", "group": "出图",
        "desc": "软件光栅渲染（不依赖显卡/GL），可选角度、分辨率、背景。",
        "needs": "model",
        "params": [
            {"k": "size", "label": "分辨率", "type": "select", "options": [512, 768, 1024, 1536], "default": 1024},
            {"k": "az", "label": "方位角 °", "type": "range", "min": 0, "max": 360, "step": 5, "default": 35},
            {"k": "el", "label": "仰角 °", "type": "range", "min": -80, "max": 80, "step": 5, "default": 18},
            {"k": "bg", "label": "背景", "type": "select",
             "options": ["深空", "纯黑", "白"], "default": "深空"},
            {"k": "style", "label": "风格", "type": "select",
             "options": ["real", "cel"], "default": "real"},
            {"k": "wire", "label": "线框叠加", "type": "switch", "default": False},
        ],
    },
    {
        "id": "measure", "name": "几何测量", "group": "出图",
        "desc": "面数、顶点、包围盒、是否水密、体积、表面积。",
        "needs": "model",
        "params": [],
    },
    {
        "id": "multiview", "name": "多视角融合", "group": "重建",
        "desc": "多张不同角度照片 → 逐张 TripoSR → open3d ICP 配准 → 合并点云 → 泊松重建。补上单图看不到的背面。",
        "needs": "images",
        "params": [
            {"k": "resolution", "label": "单图分辨率", "type": "select", "options": [192, 256, 320], "default": 256},
            {"k": "icp", "label": "ICP 精配准", "type": "switch", "default": True},
            {"k": "voxel", "label": "体素尺寸", "type": "range", "min": 4, "max": 30, "step": 1, "default": 8},
            {"k": "faces", "label": "目标面数", "type": "range", "min": 20000, "max": 300000, "step": 5000, "default": 120000},
            {"k": "smooth", "label": "平滑迭代", "type": "range", "min": 0, "max": 20, "step": 1, "default": 4},
        ],
    },
    {
        "id": "text2img", "name": "文生图 · SD-Turbo", "group": "生成",
        "desc": "文本 → SD-Turbo（本地 1 步出图）→ PNG。可先看效果，再一键转 3D。",
        "needs": "none",
        "params": [
            {"k": "prompt", "label": "描述（英文更准）", "type": "text",
             "default": "a single white ceramic teapot, product photo, plain white background"},
            {"k": "size", "label": "分辨率", "type": "select", "options": [512, 640, 768], "default": 512},
            {"k": "steps", "label": "步数", "type": "range", "min": 1, "max": 8, "step": 1, "default": 2},
            {"k": "seed", "label": "随机种子（0=随机）", "type": "range", "min": 0, "max": 99999, "step": 1, "default": 0},
        ],
    },
    {
        "id": "text3d", "name": "文生 3D", "group": "生成",
        "desc": "文本 → SD-Turbo 出图 → TripoSR → 网格。一句话生成一个 3D 物体。",
        "needs": "none",
        "params": [
            {"k": "prompt", "label": "描述（英文更准）", "type": "text",
             "default": "a single red vintage teapot, product photo, plain white background"},
            {"k": "size", "label": "中间图分辨率", "type": "select", "options": [512, 640, 768], "default": 512},
            {"k": "steps", "label": "出图步数", "type": "range", "min": 1, "max": 8, "step": 1, "default": 2},
            {"k": "resolution", "label": "网格分辨率", "type": "select", "options": [192, 256, 320], "default": 256},
            {"k": "seed", "label": "随机种子（0=随机）", "type": "range", "min": 0, "max": 99999, "step": 1, "default": 0},
        ],
    },
    {
        "id": "highpoly", "name": "高模细分", "group": "高模",
        "desc": "Loop 细分把面数翻 4 倍/级，顶点色插值保留，再做 Taubin 收边。低模 → 高模的主力手段。",
        "needs": "model",
        "params": [
            {"k": "level", "label": "细分级数", "type": "range", "min": 1, "max": 3, "step": 1, "default": 1},
            {"k": "smooth", "label": "细分后平滑", "type": "range", "min": 0, "max": 20, "step": 1, "default": 2},
            {"k": "faces_cap", "label": "面数上限（千）", "type": "range", "min": 50, "max": 800, "step": 10, "default": 300},
        ],
    },
    {
        "id": "uvbake", "name": "UV 展开 + 贴图烘焙", "group": "高模",
        "desc": "xatlas 展开 UV，把顶点色烘成贴图，输出带 UV 的 GLB + OBJ + PNG。高模进引擎的必备步骤。",
        "needs": "model",
        "params": [
            {"k": "texture", "label": "贴图尺寸", "type": "select", "options": [1024, 2048, 4096], "default": 2048},
        ],
    },
    {
        "id": "normalize", "name": "资产规范化", "group": "游戏资产",
        "desc": "XZ 居中、脚底落到 Y=0、缩放到指定身高、Y-up 朝向。进引擎前的最后一道工序。",
        "needs": "model",
        "params": [
            {"k": "height", "label": "目标身高 m", "type": "range", "min": 0.2, "max": 5, "step": 0.1, "default": 1.7},
            {"k": "flip_z", "label": "翻转朝向（-Z 朝前）", "type": "switch", "default": False},
        ],
    },
    {
        "id": "assetcheck", "name": "游戏就绪体检", "group": "游戏资产",
        "desc": "面数 / 水密 / UV / 贴图 / 尺度 / 原点 / 连通块 七项判分，给 0–100 分与待办清单。",
        "needs": "model",
        "params": [],
    },
    {
        "id": "lod", "name": "多级 LOD", "group": "游戏资产",
        "desc": "一次出 LOD0–LODn，面数逐级减半，附 lod_manifest.json（引擎导入用）。",
        "needs": "model",
        "params": [
            {"k": "levels", "label": "LOD 级数", "type": "range", "min": 2, "max": 5, "step": 1, "default": 3},
        ],
    },
    {
        "id": "blender_export", "name": "Blender 导出", "group": "游戏资产",
        "desc": "Blender 4.2 headless 导出 FBX / OBJ / USD / STL / GLB / BLEND。FBX 是 Unity / UE / 3ds Max 通用格式。",
        "needs": "model",
        "params": [
            {"k": "fmt", "label": "目标格式", "type": "select",
             "options": ["fbx", "obj", "usd", "stl", "glb", "blend"], "default": "fbx"},
        ],
    },
    {
        "id": "maya_export", "name": "Maya 导出", "group": "游戏资产",
        "desc": "Maya 2024 standalone 转成 .ma / .mb 场景文件，方便在 Maya 里继续加工。",
        "needs": "model",
        "params": [
            {"k": "fmt", "label": "Maya 格式", "type": "select", "options": ["ma", "mb"], "default": "ma"},
        ],
    },
]
PIPE_MAP = {p["id"]: p for p in PIPELINES}

# ─── 云端模式：没有 GPU / 内存小，自动隐藏重型管线 ───
CLOUD_MODE = os.environ.get("CLOUD_MODE") == "1"
GPU_PIPES = {"depth", "triposr", "text2img", "text3d", "multiview", "highpoly"}
if CLOUD_MODE:
    for _p in PIPELINES:
        if _p["id"] in GPU_PIPES:
            _p["available"] = False
            _p["reason"] = "需要 GPU，请在本地平台运行"
        else:
            _p["available"] = True



# ─────────────────────────── 软件渲染（numpy + PIL，无 GL） ───────────────────────────
def _render_png(mesh_path: str, out_path: str, size: int = 1024, az: float = 35,
                el: float = 18, bg: str = "深空", wire: bool = False,
                style: str = "real") -> dict:
    import numpy as np
    import trimesh
    from PIL import Image, ImageDraw

    mesh = trimesh.load(mesh_path, process=False, force="mesh")
    if not hasattr(mesh, "vertices"):
        raise RuntimeError("载入后不是网格")
    if len(mesh.faces) > 120000:
        try:
            import open3d as o3d
            m = o3d.geometry.TriangleMesh(
                o3d.utility.Vector3dVector(np.asarray(mesh.vertices, dtype=np.float64)),
                o3d.utility.Vector3iVector(np.asarray(mesh.faces, dtype=np.int32)))
            m = m.simplify_quadric_decimation(120000)
            mesh = trimesh.Trimesh(vertices=np.asarray(m.vertices), faces=np.asarray(m.triangles),
                                   process=False)
        except Exception:
            pass

    v = np.asarray(mesh.vertices, dtype=np.float64)
    f = np.asarray(mesh.faces, dtype=np.int64)
    if len(v) == 0 or len(f) == 0:
        raise RuntimeError("网格为空")

    # 顶点色
    vc = None
    try:
        visual = mesh.visual
        if hasattr(visual, "vertex_colors") and visual.vertex_colors is not None and len(visual.vertex_colors):
            vc = np.asarray(visual.vertex_colors)[:, :3].astype(np.float64) / 255.0
    except Exception:
        vc = None

    center = (v.max(0) + v.min(0)) / 2
    v = v - center
    span = float(np.abs(v).max()) or 1.0
    v = v / span

    ar, er = math.radians(az), math.radians(el)
    Ry = np.array([[math.cos(ar), 0, math.sin(ar)], [0, 1, 0], [-math.sin(ar), 0, math.cos(ar)]])
    Rx = np.array([[1, 0, 0], [0, math.cos(er), -math.sin(er)], [0, math.sin(er), math.cos(er)]])
    p = v @ Ry.T @ Rx.T

    # 背景
    if bg == "纯黑":
        img = Image.new("RGB", (size, size), (0, 0, 0))
    elif bg == "白":
        img = Image.new("RGB", (size, size), (245, 246, 248))
    else:
        img = Image.new("RGB", (size, size), (8, 11, 17))
        d0 = ImageDraw.Draw(img)
        for i in range(size):
            t = i / max(1, size - 1)
            d0.line([(0, i), (size, i)], fill=(int(8 + 10 * t), int(11 + 14 * t), int(17 + 22 * t)))

    dr = ImageDraw.Draw(img, "RGBA")
    pad = size * 0.06
    half = size / 2 - pad
    sx = half * p[:, 0]
    sy = -half * p[:, 2]
    depth = p[:, 1]
    px, py = size / 2 + sx, size / 2 + sy

    tri = f
    fz = depth[tri].mean(1)
    order = np.argsort(fz)  # 远 → 近

    # 光照
    n = np.cross(v[tri[:, 1]] - v[tri[:, 0]], v[tri[:, 2]] - v[tri[:, 0]])
    ln = np.linalg.norm(n, axis=1)
    ln[ln == 0] = 1e-9
    n = n / ln[:, None]
    key = np.array([0.45, 0.72, 0.53])
    key = key / np.linalg.norm(key)
    lam = np.abs(n @ key)
    fill = np.clip(0.28 + 0.62 * lam + 0.18 * np.clip(n @ np.array([-0.6, 0.2, -0.4]), 0, 1), 0, 1.25)

    if vc is not None:
        base = vc[tri].mean(1)
    else:
        base = np.array([0.80, 0.84, 0.90])

    cel = (style or "real").lower() in ("cel", "toon", "anime")
    if cel:
        # 三阶色阶（暗 / 中 / 亮），描边留到渲染后做边缘检测
        levels = np.digitize(lam, [0.30, 0.68])
        shade = np.array([0.58, 0.88, 1.18])[levels]
        cols = np.clip((np.clip(base, 0, 1) * shade[:, None]) * 255, 0, 255).astype(np.uint8)
        edge = None
    else:
        cols = np.clip((base * fill[:, None]) * 255, 0, 255).astype(np.uint8)
        edge = None

    for idx in order:
        t = tri[idx]
        pts = [(px[t[0]], py[t[0]]), (px[t[1]], py[t[1]]), (px[t[2]], py[t[2]])]
        c = tuple(int(x) for x in cols[idx])
        dr.polygon(pts, fill=c + (255,),
                   outline=(c + (255,)) if (not cel and size <= 768) else None)

    if wire:
        step = max(1, len(tri) // 4000)
        for idx in order[::step]:
            t = tri[idx]
            dr.line([(px[t[0]], py[t[0]]), (px[t[1]], py[t[1]]), (px[t[2]], py[t[2]]), (px[t[0]], py[t[0]])],
                    fill=(120, 240, 220, 60), width=1)

    if cel:
        # 边缘检测描边：只描真正的外轮廓与结构线
        arr = np.asarray(img).astype(np.float32)
        gray = arr.mean(2)
        gx = np.zeros_like(gray)
        gy = np.zeros_like(gray)
        gx[:, :-1] = np.abs(np.diff(gray, axis=1))
        gy[:-1, :] = np.abs(np.diff(gray, axis=0))
        mag = gx + gy
        edge_mask = mag > max(22.0, float(np.percentile(mag, 99.4)))
        arr[edge_mask] = np.array([26, 28, 36], dtype=np.float32)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    img.save(out_path)
    return {"png": out_path, "faces": int(len(f)), "size": size}


# ─────────────────────────── 管线实现（在 worker 里跑） ───────────────────────────
def _load_mesh(path: str):
    import numpy as np
    import trimesh
    import open3d as o3d
    m = trimesh.load(path, process=False, force="mesh")
    if not hasattr(m, "vertices") or len(m.faces) == 0:
        raise RuntimeError("不是有效网格")
    colors = None
    try:
        v = m.visual
        if hasattr(v, "vertex_colors") and v.vertex_colors is not None and len(v.vertex_colors):
            colors = np.asarray(v.vertex_colors)[:, :3]
    except Exception:
        colors = None
    mesh = o3d.geometry.TriangleMesh(
        o3d.utility.Vector3dVector(np.asarray(m.vertices, dtype=np.float64)),
        o3d.utility.Vector3iVector(np.asarray(m.faces, dtype=np.int32)))
    if colors is not None:
        mesh.vertex_colors = o3d.utility.Vector3dVector(colors.astype(np.float64) / 255.0)
    return mesh


def _export(mesh, out_dir: Path, fmt: str, binary: bool = False, stem: str = "model") -> str:
    import numpy as np
    import open3d as o3d
    import trimesh
    verts = np.asarray(mesh.vertices)
    faces = np.asarray(mesh.triangles)
    cols = None
    if mesh.has_vertex_colors():
        cols = (np.asarray(mesh.vertex_colors) * 255).astype(np.uint8)
    kwargs = {}
    if cols is not None and fmt in ("glb", "gltf", "ply", "obj"):
        kwargs["vertex_colors"] = cols
    tm = trimesh.Trimesh(vertices=verts, faces=faces, process=False, **kwargs)
    p = out_dir / f"{stem}.{fmt}"
    tm.export(str(p))
    return p.name


def _stats(mesh, path_tri) -> dict:
    import numpy as np
    import trimesh
    bb = mesh.get_axis_aligned_bounding_box()
    ext = bb.get_extent()
    tm = path_tri
    out = {
        "verts": int(len(mesh.vertices)),
        "tris": int(len(mesh.triangles)),
        "size": [round(float(x), 3) for x in ext],
        "watertight": bool(tm.is_watertight),
        "volume": round(float(abs(tm.volume)), 4) if tm.is_volume else None,
        "area": round(float(tm.area), 4),
        "components": int(len(tm.split(only_watertight=False))) if len(tm.faces) < 200000 else None,
    }
    return out


def pipe_depth(img_path: Path, out_dir: Path, params: dict, progress) -> dict:
    import numpy as np
    import open3d as o3d
    import trimesh
    from PIL import Image

    cache = params.pop("_cache")
    if "depth" not in cache:
        progress("加载深度模型", 5)
        import torch
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation
        mid = "depth-anything/Depth-Anything-V2-Small-hf"
        proc = AutoImageProcessor.from_pretrained(mid)
        model = AutoModelForDepthEstimation.from_pretrained(mid).eval()
        if torch.cuda.is_available():
            model = model.cuda()
        cache["depth"] = (proc, model, torch)
    proc, model, torch = cache["depth"]

    faces = int(params.get("faces", 25000))
    smooth = int(params.get("smooth", 18))
    fov = float(params.get("fov", 50))
    near = float(params.get("near", 0.5))
    far = float(params.get("far", 8))

    progress("深度估计", 12)
    img = Image.open(img_path).convert("RGB")
    W0, H0 = img.size
    inp = proc(images=img, return_tensors="pt")
    if torch.cuda.is_available():
        inp = {k: v.cuda() for k, v in inp.items()}
    with torch.no_grad():
        depth = model(**inp).predicted_depth
    depth = torch.nn.functional.interpolate(depth.unsqueeze(1), size=(H0, W0),
                                            mode="bicubic", align_corners=False).squeeze().cpu().numpy()
    d = (depth - depth.min()) / (depth.max() - depth.min() + 1e-8)

    progress("反投影点云", 32)
    rgb = np.asarray(img).astype(np.float32) / 255
    h, w = d.shape
    f = 0.5 * w / np.tan(np.deg2rad(fov) / 2)
    xs, ys = np.mgrid[0:h, 0:w]
    z = near + (1.0 - d) * (far - near)
    X = (xs - w / 2) * z / f
    Y = (ys - h / 2) * z / f
    pts = np.stack([X, Y, z], -1).reshape(-1, 3)
    cols = rgb.reshape(-1, 3)
    mask = z.ravel() > np.percentile(z.ravel(), 6)
    pts, cols = pts[mask], cols[mask]

    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(pts)
    pcd.colors = o3d.utility.Vector3dVector(cols)
    pcd, _ = pcd.remove_statistical_outlier(28, 1.5)
    pcd.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=0.30, max_nn=50))
    pcd.orient_normals_consistent_tangent_plane(30)

    progress("泊松重建", 58)
    mesh, dens = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=9, linear_fit=True)
    dens = np.asarray(dens)
    mesh.remove_vertices_by_mask(dens < np.quantile(dens, 0.05))
    mesh.compute_vertex_normals()

    progress("平滑简化", 78)
    mesh = mesh.filter_smooth_taubin(number_of_iterations=max(1, smooth), lambda_filter=0.55)
    mesh.compute_vertex_normals()
    if len(mesh.triangles) > faces:
        mesh = mesh.simplify_quadric_decimation(faces)
        mesh.compute_vertex_normals()
        mesh = mesh.filter_smooth_taubin(number_of_iterations=4, lambda_filter=0.5)
        mesh.compute_vertex_normals()

    progress("导出", 92)
    glb = _export(mesh, out_dir, "glb")
    _export(mesh, out_dir, "ply")
    shutil.copy2(img_path, out_dir / ("source" + img_path.suffix))
    t = trimesh.load(str(out_dir / glb), process=False, force="mesh")
    st = _stats(mesh, t)
    return {"ok": True, "glb": glb, **st}


def pipe_triposr(img_path: Path, out_dir: Path, params: dict, progress) -> dict:
    import numpy as np
    import torch
    from PIL import Image

    cache = params.pop("_cache")
    progress("加载 TripoSR", 6)
    if TRIPOSR_DIR.exists():
        p = str(TRIPOSR_DIR)
        if p not in sys.path:
            sys.path.insert(0, p)
    from tsr.system import TSR

    if "triposr" not in cache:
        ckpt = next((c for c in TRIPOSR_CANDIDATES if c.exists()), None)
        if ckpt is None:
            raise RuntimeError("找不到 TripoSR 权重（model_fixed.ckpt）")
        model = TSR.from_pretrained(str(TRIPOSR_DIR / "model_local"),
                                    config_name="config.yaml", weight_name=ckpt.name) \
            if ckpt.parent == TRIPOSR_DIR / "model_local" else \
            _triposr_from_path(str(TRIPOSR_DIR / "model_local"), str(ckpt))
        model.renderer.set_chunk_size(131072)
        model.to("cuda" if torch.cuda.is_available() else "cpu")
        cache["triposr"] = model
    model = cache["triposr"]
    dev = "cuda" if torch.cuda.is_available() else "cpu"

    res = int(params.get("resolution", 256))
    thr = float(params.get("threshold", 25))
    use_color = bool(params.get("vertex_color", True))
    do_rembg = bool(params.get("rembg", False))

    progress("读图预处理", 18)
    img = Image.open(img_path).convert("RGB")
    if do_rembg:
        try:
            from rembg import remove
            img = remove(img).convert("RGBA")
            bgc = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bgc, img).convert("RGB")
        except Exception:
            pass

    progress("前馈推理", 34)
    with torch.no_grad():
        codes = model([img], device=dev)

    progress(f"提取网格 res={res}", 62)
    with torch.no_grad():
        meshes = model.extract_mesh(codes, has_vertex_color=use_color, resolution=res, threshold=thr)
    m = meshes[0]

    progress("清理悬空碎块", 80)
    m = _keep_main_parts(m, float(params.get("min_part", 0.02)))

    progress("导出", 90)
    import trimesh
    glb_path = out_dir / "model.glb"
    m.export(str(glb_path))
    m.export(str(out_dir / "model.ply"))
    shutil.copy2(img_path, out_dir / ("source" + img_path.suffix))
    st = _stats_from_trimesh(m)
    return {"ok": True, "glb": "model.glb",
            "kb": round(glb_path.stat().st_size / 1024), **st}


def _triposr_from_path(model_dir: str, ckpt_path: str):
    """权重在别处时，复用 TSR.from_pretrained 的目录分支。"""
    import torch
    from omegaconf import OmegaConf
    sys.path.insert(0, str(TRIPOSR_DIR))
    from tsr.system import TSR
    cfg = OmegaConf.load(os.path.join(model_dir, "config.yaml"))
    OmegaConf.resolve(cfg)
    model = TSR(cfg)
    ck = torch.load(ckpt_path, map_location="cpu")
    if hasattr(ck, "state_dict"):
        ck = ck.state_dict()
    model.load_state_dict(ck)
    return model


def _keep_main_parts(tm, min_frac: float = 0.02):
    """去掉悬空碎块：按「最大连通块面积」的比例过滤，至少保留主体。"""
    try:
        import trimesh
        parts = tm.split(only_watertight=False)
        if len(parts) <= 1:
            return tm
        areas = [p.area for p in parts]
        maxa = max(areas) or 1.0
        keep = [p for p, a in zip(parts, areas) if a >= maxa * max(0.05, min_frac)]
        if not keep:
            keep = [max(parts, key=lambda p: p.area)]
        if len(keep) == len(parts):
            return tm
        return trimesh.util.concatenate(keep)
    except Exception:
        return tm


def _stats_from_trimesh(tm) -> dict:
    import numpy as np
    v = np.asarray(tm.vertices)
    ext = v.max(0) - v.min(0) if len(v) else np.zeros(3)
    return {
        "verts": int(len(v)),
        "tris": int(len(tm.faces)),
        "size": [round(float(x), 3) for x in ext],
        "watertight": bool(tm.is_watertight),
        "volume": round(float(abs(tm.volume)), 4) if tm.is_volume else None,
        "area": round(float(tm.area), 4),
        "components": int(len(tm.split(only_watertight=False))) if len(tm.faces) < 200000 else None,
    }


def pipe_simplify(src: Path, out_dir: Path, params: dict, progress) -> dict:
    import trimesh
    progress("载入网格", 10)
    mesh = _load_mesh(str(src))
    target = int(params.get("faces", 20000))
    progress("二次误差简化", 40)
    if len(mesh.triangles) > target:
        mesh = mesh.simplify_quadric_decimation(target)
    sa = int(params.get("smooth_after", 3))
    if sa > 0:
        progress("平滑收边", 70)
        mesh = mesh.filter_smooth_taubin(number_of_iterations=sa, lambda_filter=0.5)
    mesh.compute_vertex_normals()
    progress("导出", 88)
    glb = _export(mesh, out_dir, "glb")
    tl = trimesh.load(str(out_dir / glb), process=False, force="mesh")
    return {"ok": True, "glb": glb, **_stats(mesh, tl)}


def pipe_smooth(src: Path, out_dir: Path, params: dict, progress) -> dict:
    import trimesh
    progress("载入网格", 10)
    mesh = _load_mesh(str(src))
    it = int(params.get("iterations", 8))
    lam = float(params.get("lambda", 0.5))
    progress("Taubin 平滑", 45)
    mesh = mesh.filter_smooth_taubin(number_of_iterations=it, lambda_filter=lam)
    mesh.compute_vertex_normals()
    progress("导出", 88)
    glb = _export(mesh, out_dir, "glb")
    tl = trimesh.load(str(out_dir / glb), process=False, force="mesh")
    return {"ok": True, "glb": glb, **_stats(mesh, tl)}


def pipe_repair(src: Path, out_dir: Path, params: dict, progress) -> dict:
    import numpy as np
    import open3d as o3d
    import trimesh
    progress("载入网格", 10)
    mesh = _load_mesh(str(src))
    mesh.remove_duplicated_vertices()
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    if params.get("unify_normals", True):
        progress("统一法线", 35)
        mesh.orient_triangles()
    small = int(params.get("remove_small", 300))
    if small > 0 and len(mesh.triangles) > small * 2:
        progress("拆分并清理碎块", 55)
        tm = trimesh.Trimesh(vertices=np.asarray(mesh.vertices),
                             faces=np.asarray(mesh.triangles), process=False)
        parts = tm.split(only_watertight=False)
        if len(parts) > 1:
            keep = [p for p in parts if len(p.faces) >= small]
            if keep:
                tm = trimesh.util.concatenate(keep)
                mesh = o3d.geometry.TriangleMesh(
                    o3d.utility.Vector3dVector(np.asarray(tm.vertices)),
                    o3d.utility.Vector3iVector(np.asarray(tm.faces)))
                mesh.compute_vertex_normals()
    if params.get("fill_holes", True):
        progress("补洞", 72)
        try:
            mesh = mesh.fill_holes(hole_size=1e6)
        except Exception:
            pass
    mesh.compute_vertex_normals()
    progress("导出", 90)
    glb = _export(mesh, out_dir, "glb")
    tl = trimesh.load(str(out_dir / glb), process=False, force="mesh")
    return {"ok": True, "glb": glb, **_stats(mesh, tl)}


def pipe_convert(src: Path, out_dir: Path, params: dict, progress) -> dict:
    import trimesh
    fmt = str(params.get("fmt", "obj")).lower()
    progress("载入网格", 20)
    tm = trimesh.load(str(src), process=False, force="mesh")
    progress(f"写出 {fmt.upper()}", 60)
    p = out_dir / f"converted.{fmt}"
    tm.export(str(p))
    st = _stats_from_trimesh(tm)
    return {"ok": True, "file": p.name, "fmt": fmt, "kb": round(p.stat().st_size / 1024), **st}


def pipe_render(src: Path, out_dir: Path, params: dict, progress) -> dict:
    progress("载入网格", 20)
    size = int(params.get("size", 1024))
    progress("光栅渲染", 55)
    out = out_dir / f"render_{int(params.get('az', 35))}_{int(params.get('el', 18))}.png"
    info = _render_png(str(src), str(out), size=size, az=float(params.get("az", 35)),
                       el=float(params.get("el", 18)), bg=str(params.get("bg", "深空")),
                       wire=bool(params.get("wire", False)),
                       style=str(params.get("style", "real")))
    return {"ok": True, "png": out.name, "size_px": size, "faces_drawn": info["faces"],
            "style": str(params.get("style", "real"))}


def pipe_measure(src: Path, out_dir: Path, params: dict, progress) -> dict:
    import trimesh
    progress("载入网格", 30)
    tm = trimesh.load(str(src), process=False, force="mesh")
    progress("统计", 70)
    st = _stats_from_trimesh(tm)
    return {"ok": True, "measured": st}


# ─────────────────────────── 多视角 / 文生 / 高模 ───────────────────────────
SYSTEM_PY = r"C:\Users\MN\AppData\Local\Programs\Python\Python312\python.exe"
SD_SCRIPT = ROOT / "scripts" / "sd_generate.py"
SD_DIR = Path(r"D:\DSH视频\models\sd-turbo")


def _ensure_triposr(cache, progress):
    """懒加载 TripoSR，供多视角与文生 3D 复用。"""
    import torch
    if "triposr" in cache:
        return cache["triposr"]
    if TRIPOSR_DIR.exists():
        p = str(TRIPOSR_DIR)
        if p not in sys.path:
            sys.path.insert(0, p)
    from tsr.system import TSR
    ckpt = next((c for c in TRIPOSR_CANDIDATES if c.exists()), None)
    if ckpt is None:
        raise RuntimeError("找不到 TripoSR 权重")
    progress("加载 TripoSR", 8)
    if ckpt.parent == TRIPOSR_DIR / "model_local":
        model = TSR.from_pretrained(str(TRIPOSR_DIR / "model_local"),
                                    config_name="config.yaml", weight_name=ckpt.name)
    else:
        model = _triposr_from_path(str(TRIPOSR_DIR / "model_local"), str(ckpt))
    model.renderer.set_chunk_size(131072)
    model.to("cuda" if torch.cuda.is_available() else "cpu")
    cache["triposr"] = model
    return model


def _sd_generate(prompt, out_path, size=512, steps=2, seed=0, timeout=900):
    import subprocess
    if not (SD_DIR / "model_index.json").exists():
        raise RuntimeError("SD-Turbo 权重未就绪（D:\\DSH视频\\models\\sd-turbo）")
    cfg = json.dumps({"prompt": prompt, "out": str(out_path), "size": int(size),
                      "steps": int(steps), "seed": int(seed)}, ensure_ascii=False)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([SYSTEM_PY, str(SD_SCRIPT), cfg], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=env)
    if r.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(f"SD 生成失败：{(r.stdout or '')[-300:]} | {(r.stderr or '')[-600:]}")
    return out_path


def pipe_multiview(srcs, out_dir: Path, params: dict, progress) -> dict:
    import numpy as np
    import open3d as o3d
    import torch
    import trimesh
    from PIL import Image

    cache = params.pop("_cache")
    if not srcs:
        raise RuntimeError("至少需要一张图")
    res = int(params.get("resolution", 256))
    use_icp = bool(params.get("icp", True))
    voxel = max(0.002, float(params.get("voxel", 8)) / 1000.0)
    faces = int(params.get("faces", 120000))
    smooth = int(params.get("smooth", 4))
    model = _ensure_triposr(cache, progress)
    dev = "cuda" if torch.cuda.is_available() else "cpu"

    n = len(srcs)
    meshes = []
    for i, p in enumerate(srcs):
        progress(f"视角 {i+1}/{n} 重建", 15 + int(45 * i / max(1, n)))
        img = Image.open(p).convert("RGB")
        with torch.no_grad():
            codes = model([img], device=dev)
        with torch.no_grad():
            m = model.extract_mesh(codes, has_vertex_color=True, resolution=res, threshold=25.0)[0]
        meshes.append(m)
        try:
            m.export(str(out_dir / f"view_{i}.glb"))
        except Exception:
            pass

    progress("采样点云", 64)
    pcds = []
    for m in meshes:
        pts, fidx = trimesh.sample.sample_surface(m, 30000)
        fn = m.face_normals[fidx]
        try:
            cols = np.asarray(m.visual.vertex_colors)[m.faces[fidx]].mean(1)[:, :3] / 255.0
        except Exception:
            cols = np.tile([0.7, 0.72, 0.76], (len(pts), 1))
        pc = o3d.geometry.PointCloud()
        pc.points = o3d.utility.Vector3dVector(pts)
        pc.normals = o3d.utility.Vector3dVector(fn)
        pc.colors = o3d.utility.Vector3dVector(cols)
        pcds.append(pc)

    progress("ICP 配准", 72)
    merged = pcds[0]
    for pc in pcds[1:]:
        if use_icp:
            try:
                reg = o3d.pipelines.registration.registration_icp(
                    pc, merged, 0.12, np.eye(4),
                    o3d.pipelines.registration.TransformationEstimationPointToPlane(),
                    o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=50))
                pc.transform(reg.transformation)
            except Exception:
                pass
        merged += pc

    progress("融合 + 泊松重建", 82)
    merged = merged.voxel_down_sample(voxel)
    merged.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 6, max_nn=40))
    try:
        merged.orient_normals_consistent_tangent_plane(30)
    except Exception:
        pass
    mesh, dens = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
        merged, depth=9, linear_fit=True)
    dens = np.asarray(dens)
    mesh.remove_vertices_by_mask(dens < np.quantile(dens, 0.03))
    if smooth:
        mesh = mesh.filter_smooth_taubin(number_of_iterations=smooth, lambda_filter=0.5)
    mesh.compute_vertex_normals()
    if len(mesh.triangles) > faces:
        mesh = mesh.simplify_quadric_decimation(faces)
        mesh.compute_vertex_normals()

    progress("导出", 92)
    glb = _export(mesh, out_dir, "glb")
    tl = trimesh.load(str(out_dir / glb), process=False, force="mesh")
    return {"ok": True, "glb": glb, "views": n,
            "kb": round((out_dir / glb).stat().st_size / 1024), **_stats(mesh, tl)}


def pipe_text2img(src, out_dir: Path, params: dict, progress) -> dict:
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("描述不能为空")
    progress("SD-Turbo 出图", 30)
    out = out_dir / "generated.png"
    _sd_generate(prompt, out, params.get("size", 512), params.get("steps", 2), params.get("seed", 0))
    progress("完成", 95)
    return {"ok": True, "png": out.name, "prompt": prompt,
            "kb": round(out.stat().st_size / 1024), "size_px": int(params.get("size", 512))}


def pipe_text3d(src, out_dir: Path, params: dict, progress) -> dict:
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("描述不能为空")
    cache = params.pop("_cache")
    progress("SD-Turbo 出图", 12)
    out = out_dir / "generated.png"
    _sd_generate(prompt, out, params.get("size", 512), params.get("steps", 2), params.get("seed", 0))
    progress("图生 3D", 45)
    res = pipe_triposr(out, out_dir, {
        "resolution": int(params.get("resolution", 256)),
        "threshold": 25.0, "vertex_color": True, "rembg": False, "_cache": cache,
    }, progress)
    res["prompt"] = prompt
    res["png"] = "generated.png"
    return res


def pipe_highpoly(src: Path, out_dir: Path, params: dict, progress) -> dict:
    import numpy as np
    import trimesh

    progress("载入网格", 8)
    tm = trimesh.load(str(src), process=False, force="mesh")
    level = int(params.get("level", 1))
    cap = int(params.get("faces_cap", 300)) * 1000
    before = len(tm.faces)

    for i in range(level):
        if len(tm.faces) * 4 > cap * 1.15:
            break
        progress(f"Loop 细分 {i+1}/{level}", 25 + i * 20)
        try:
            tm = tm.subdivide()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"细分失败：{e}")
        if len(tm.visual.vertex_colors) != len(tm.vertices):
            try:
                tm.visual.vertex_colors = np.tile(
                    np.array([184, 192, 204, 255], dtype=np.uint8), (len(tm.vertices), 1))
            except Exception:
                pass

    sm = int(params.get("smooth", 2))
    if sm:
        progress("Taubin 收边", 78)
        try:
            import open3d as o3d
            m = o3d.geometry.TriangleMesh(
                o3d.utility.Vector3dVector(np.asarray(tm.vertices, dtype=np.float64)),
                o3d.utility.Vector3iVector(np.asarray(tm.faces, dtype=np.int32)))
            m = m.filter_smooth_taubin(number_of_iterations=sm, lambda_filter=0.5)
            m.compute_vertex_normals()
            tm = trimesh.Trimesh(vertices=np.asarray(m.vertices),
                                 faces=np.asarray(m.triangles), process=False)
        except Exception:
            pass

    if len(tm.faces) > cap:
        progress("压回面数上限", 84)
        try:
            import open3d as o3d
            m = o3d.geometry.TriangleMesh(
                o3d.utility.Vector3dVector(np.asarray(tm.vertices, dtype=np.float64)),
                o3d.utility.Vector3iVector(np.asarray(tm.faces, dtype=np.int32)))
            m = m.simplify_quadric_decimation(cap)
            tm = trimesh.Trimesh(vertices=np.asarray(m.vertices),
                                 faces=np.asarray(m.triangles), process=False)
        except Exception:
            pass

    progress("导出", 92)
    glb = out_dir / "model.glb"
    tm.export(str(glb))
    st = _stats_from_trimesh(tm)
    st["faces_before"] = int(before)
    return {"ok": True, "glb": "model.glb",
            "kb": round(glb.stat().st_size / 1024), **st}


def _bake_uv_texture(verts, faces, uvs, colors, size):
    """把顶点色按 UV 光栅化到贴图（重心坐标插值）。"""
    import numpy as np
    from PIL import Image

    tex = np.zeros((size, size, 3), dtype=np.float32)
    mask = np.zeros((size, size), dtype=bool)
    uv = np.asarray(uvs, dtype=np.float64).copy()
    uv[:, 1] = 1.0 - uv[:, 1]
    px = uv * (size - 1)
    cols = np.asarray(colors, dtype=np.float64)

    step = max(1, len(faces) // 40000)
    for fi in range(0, len(faces), step):
        i0, i1, i2 = faces[fi]
        p = px[[i0, i1, i2]]
        xmin = max(0, int(np.floor(p[:, 0].min())))
        xmax = min(size - 1, int(np.ceil(p[:, 0].max())))
        ymin = max(0, int(np.floor(p[:, 1].min())))
        ymax = min(size - 1, int(np.ceil(p[:, 1].max())))
        if xmax < xmin or ymax < ymin:
            continue
        xs, ys = np.meshgrid(np.arange(xmin, xmax + 1), np.arange(ymin, ymax + 1))
        d = ((p[1, 1] - p[2, 1]) * (p[0, 0] - p[2, 0]) +
             (p[2, 0] - p[1, 0]) * (p[0, 1] - p[2, 1]))
        if abs(d) < 1e-9:
            continue
        w0 = ((p[1, 1] - p[2, 1]) * (xs - p[2, 0]) + (p[2, 0] - p[1, 0]) * (ys - p[2, 1])) / d
        w1 = ((p[2, 1] - p[0, 1]) * (xs - p[2, 0]) + (p[0, 0] - p[2, 0]) * (ys - p[2, 1])) / d
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -0.003) & (w1 >= -0.003) & (w2 >= -0.003)
        if not inside.any():
            continue
        c = (w0[..., None] * cols[i0] + w1[..., None] * cols[i1] + w2[..., None] * cols[i2])
        ys_i, xs_i = ys[inside], xs[inside]
        tex[ys_i, xs_i] = np.clip(c[inside], 0, 1)
        mask[ys_i, xs_i] = True

    # 填充未覆盖像素（简单膨胀两轮）
    for _ in range(2):
        m = ~mask
        if not m.any():
            break
        acc = np.zeros_like(tex)
        cnt = np.zeros((size, size), dtype=np.float32)
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            shifted = np.roll(np.roll(tex, dy, 0), dx, 1)
            smask = np.roll(np.roll(mask, dy, 0), dx, 1)
            acc += shifted * smask[..., None]
            cnt += smask
        fill = m & (cnt > 0)
        tex[fill] = acc[fill] / cnt[fill][..., None]
        mask = mask | fill

    arr = (np.clip(tex, 0, 1) * 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def pipe_uvbake(src: Path, out_dir: Path, params: dict, progress) -> dict:
    import numpy as np
    import trimesh
    import xatlas

    tex_size = int(params.get("texture", 2048))
    progress("载入网格", 10)
    tm = trimesh.load(str(src), process=False, force="mesh")
    v = np.asarray(tm.vertices, dtype=np.float32)
    f = np.asarray(tm.faces, dtype=np.uint32)
    if len(f) > 120000:
        progress("面数过多，先简化到 12 万", 16)
        try:
            import open3d as o3d
            m = o3d.geometry.TriangleMesh(
                o3d.utility.Vector3dVector(v.astype(np.float64)),
                o3d.utility.Vector3iVector(f.astype(np.int32)))
            m = m.simplify_quadric_decimation(120000)
            v = np.asarray(m.vertices, dtype=np.float32)
            f = np.asarray(m.triangles, dtype=np.uint32)
            tm = trimesh.Trimesh(vertices=v, faces=f, process=False)
        except Exception:
            pass
    try:
        cols = np.asarray(tm.visual.vertex_colors)[:, :3].astype(np.float32) / 255.0
    except Exception:
        cols = np.tile(np.array([0.72, 0.75, 0.8], dtype=np.float32), (len(v), 1))

    progress("xatlas 展开 UV", 32)
    atlas = xatlas.Atlas()
    atlas.add_mesh(v, f)
    atlas.generate(xatlas.ChartOptions(), xatlas.PackOptions())
    vmapping, indices, uvs = atlas[0]
    nv = v[vmapping]
    nf = np.asarray(indices, dtype=np.uint32)
    nuv = np.asarray(uvs, dtype=np.float32)
    nc = cols[vmapping]

    progress(f"烘焙 {tex_size}² 贴图", 58)
    img = _bake_uv_texture(nv, nf, nuv, nc, tex_size)
    tex_path = out_dir / "texture.png"
    img.save(tex_path)

    progress("导出 GLB + OBJ（自带写出器，不吃内存）", 86)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from glb_writer import write_glb, write_obj
    png_bytes = tex_path.read_bytes()
    glb = out_dir / "model.glb"
    write_glb(str(glb), nv, nf, nuv, png_bytes)
    write_obj(str(out_dir / "model.obj"), nv, nf, nuv, "texture.png")
    import trimesh as _tm
    st = _stats_from_trimesh(_tm.Trimesh(vertices=nv, faces=nf, process=False))
    return {"ok": True, "glb": "model.glb",
            "files": ["model.glb", "model.obj", "model.mtl", "texture.png"],
            "texture": "texture.png",
            "kb": round(glb.stat().st_size / 1024),
            "tex_kb": round(tex_path.stat().st_size / 1024), **st}


def pipe_normalize(src: Path, out_dir: Path, params: dict, progress) -> dict:
    """游戏资产规范化：XZ 居中、脚底落在 Y=0、缩放到指定身高、Y-up。"""
    import numpy as np
    import trimesh

    progress("载入网格", 15)
    tm = trimesh.load(str(src), process=False, force="mesh")
    v = np.asarray(tm.vertices, dtype=np.float64).copy()
    if len(v) == 0:
        raise RuntimeError("网格为空")

    height = float(params.get("height", 1.7))
    progress("居中 + 落地 + 缩放", 50)
    mn, mx = v.min(0), v.max(0)
    cx, cz = (mn[0] + mx[0]) / 2.0, (mn[2] + mx[2]) / 2.0
    v[:, 0] -= cx
    v[:, 2] -= cz
    v[:, 1] -= mn[1]
    src_h = float(max(v[:, 1].max(), 1e-6))
    scale = height / src_h
    v *= scale

    if params.get("flip_z", False):
        v[:, 2] *= -1

    tm2 = trimesh.Trimesh(vertices=v, faces=np.asarray(tm.faces), process=False)
    try:
        vc = np.asarray(tm.visual.vertex_colors)
        if len(vc) == len(v):
            tm2.visual.vertex_colors = vc
    except Exception:
        pass

    progress("导出", 88)
    glb = out_dir / "model.glb"
    tm2.export(str(glb))
    st = _stats_from_trimesh(tm2)
    return {"ok": True, "glb": "model.glb", "kb": round(glb.stat().st_size / 1024),
            "height_m": round(float(height), 3), "scale_applied": round(float(scale), 4),
            "source_height": round(src_h, 3), **st}


def pipe_assetcheck(src: Path, out_dir: Path, params: dict, progress) -> dict:
    """游戏/动漫资产就绪度体检，给出 0–100 分与待办清单。"""
    import numpy as np
    import trimesh

    progress("载入网格", 20)
    tm = trimesh.load(str(src), process=False, force="mesh")
    v = np.asarray(tm.vertices)
    f = np.asarray(tm.faces)
    tris = int(len(f))
    verts = int(len(v))
    ext = (v.max(0) - v.min(0)) if len(v) else np.zeros(3)

    has_uv = False
    has_tex = False
    has_vcolor = False
    try:
        uv = getattr(tm.visual, "uv", None)
        has_uv = uv is not None and len(np.asarray(uv)) > 0
    except Exception:
        pass
    try:
        img = getattr(tm.visual, "material", None)
        has_tex = img is not None and getattr(img, "image", None) is not None
    except Exception:
        pass
    try:
        has_vcolor = len(np.asarray(tm.visual.vertex_colors)) == verts
    except Exception:
        pass

    progress("逐项判定", 55)
    items = []
    score = 0

    # 面数（游戏角色 5k–100k，影视可到 500k）
    if tris <= 100000:
        score += 25; items.append(("面数", f"{tris:,}", "ok", "适合实时游戏"))
    elif tris <= 500000:
        score += 16; items.append(("面数", f"{tris:,}", "warn", "偏高，建议先简化到 10 万以下"))
    else:
        score += 6; items.append(("面数", f"{tris:,}", "bad", "远超实时预算，必须简化"))

    # 水密
    if tm.is_watertight:
        score += 15; items.append(("水密", "是", "ok", "可用于物理/3D 打印"))
    else:
        items.append(("水密", "否", "warn", "有破洞，进引擎前建议跑一次「修复」"))

    # UV
    if has_uv:
        score += 15; items.append(("UV", "已展开", "ok", "可贴图"))
    else:
        items.append(("UV", "无", "bad", "没有 UV 就没法用贴图，跑一次「UV 展开 + 贴图烘焙」"))

    # 贴图 / 顶点色
    if has_tex:
        score += 15; items.append(("贴图", "有", "ok", "带 baseColor 贴图"))
    elif has_vcolor:
        score += 8; items.append(("着色", "仅顶点色", "warn", "顶点色在多数引擎里不能直接替代贴图"))
    else:
        items.append(("着色", "无", "bad", "白模，需要顶点色或贴图"))

    # 尺度
    h = float(ext[1]) if len(ext) else 0.0
    if 0.3 <= h <= 4.0:
        score += 15; items.append(("尺度", f"{h:.2f} m", "ok", "接近真实人体/道具尺度"))
    else:
        score += 5; items.append(("尺度", f"{h:.2f} m", "warn", "建议用「资产规范化」缩放到目标身高"))

    # 原点 / 朝向
    if len(v):
        mn = v.min(0)
        if abs(float(mn[1])) < 0.02 * max(h, 1e-6) + 0.02:
            score += 8; items.append(("原点", "脚底在 Y=0", "ok", "可直接落地摆放"))
        else:
            items.append(("原点", f"Y={mn[1]:.3f}", "warn", "脚底不在地面，跑一次「资产规范化」"))

    # 连通块
    try:
        parts = len(tm.split(only_watertight=False))
    except Exception:
        parts = 1
    if parts <= 3:
        score += 7; items.append(("连通块", str(parts), "ok", "结构干净"))
    else:
        items.append(("连通块", str(parts), "warn", "碎块偏多，建议「修复」清理"))

    grade = "A（可直接进引擎）" if score >= 85 else \
            "B（小修可用）" if score >= 65 else \
            "C（需要加工）" if score >= 40 else "D（白模/不可用）"

    return {"ok": True, "measured": {
        "tris": tris, "verts": verts,
        "size": [round(float(x), 3) for x in ext],
        "score": int(score), "grade": grade,
        "items": [{"名": a, "值": b, "级别": c, "建议": d} for a, b, c, d in items],
    }}


def pipe_lod(src: Path, out_dir: Path, params: dict, progress) -> dict:
    """一次生成多级 LOD（LOD0 原样 + 逐级减面），游戏/引擎常用。"""
    import json as _json
    import numpy as np
    import trimesh

    levels = int(params.get("levels", 3))
    levels = max(2, min(5, levels))
    ratios = [1.0] + [round(0.5 ** i, 3) for i in range(1, levels)]
    progress("载入网格", 10)
    tm = trimesh.load(str(src), process=False, force="mesh")
    base = int(len(tm.faces))
    made = []
    for i, r in enumerate(ratios):
        target = max(200, int(base * r))
        progress(f"LOD{i} → {target:,} 面", 25 + i * 20)
        if i == 0:
            cur = tm
        else:
            m = tm.copy()
            try:
                cur = m.simplify_quadric_decimation(target)
            except Exception:
                try:
                    import open3d as o3d
                    o = o3d.geometry.TriangleMesh(
                        o3d.utility.Vector3dVector(np.asarray(m.vertices, dtype=np.float64)),
                        o3d.utility.Vector3iVector(np.asarray(m.faces, dtype=np.int32)))
                    o = o.simplify_quadric_decimation(target)
                    cur = trimesh.Trimesh(vertices=np.asarray(o.vertices),
                                          faces=np.asarray(o.triangles), process=False)
                except Exception:
                    continue
        p = out_dir / f"lod{i}.glb"
        cur.export(str(p))
        made.append({"level": f"LOD{i}", "file": p.name, "tris": int(len(cur.faces)),
                     "kb": round(p.stat().st_size / 1024)})
    if not made:
        raise RuntimeError("LOD 生成失败")
    (out_dir / "lod_manifest.json").write_text(
        _json.dumps({"base_faces": base, "levels": made}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    st = _stats_from_trimesh(tm)
    st["lod"] = made
    return {"ok": True, "glb": "lod0.glb", "files": [m["file"] for m in made],
            "kb": made[0]["kb"], "measured": st}


# ─────────────── Blender / Maya 外部工具 ───────────────
BLENDER_EXE = Path(r"D:\dsh-plugins\_tools\blender\blender-4.2.3-windows-x64\blender.exe")
BLENDER_SCRIPT = ROOT / "blender_ops.py"
MAYAPY_EXE = Path(r"D:\maya\Maya2024\bin\mayapy.exe")


def pipe_blender_export(src: Path, out_dir: Path, params: dict, progress) -> dict:
    """GLB/OBJ 等 → FBX/OBJ/USD/STL/BLEND，走 Blender 4.2 headless。"""
    import subprocess
    fmt = str(params.get("fmt", "fbx")).lower()
    if not BLENDER_EXE.exists():
        raise RuntimeError(f"Blender 不存在：{BLENDER_EXE}")
    if not BLENDER_SCRIPT.exists():
        raise RuntimeError("缺少 blender_ops.py")
    progress("启动 Blender", 15)
    dst = out_dir / f"export.{fmt}"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run(
        [str(BLENDER_EXE), "--background", "--python", str(BLENDER_SCRIPT),
         "--", str(src), str(dst), fmt],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    if not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(f"Blender 导出失败：{out[-600:]}")
    tris = 0
    for line in out.splitlines():
        if line.startswith("BLENDER_OK"):
            # 形如：BLENDER_OK <path> <bytes> bytes <tris> tris
            parts = line.split()
            for i, tok in enumerate(parts):
                if tok == "tris" and i > 0 and parts[i - 1].isdigit():
                    tris = int(parts[i - 1])
                    break
    progress("完成", 92)
    return {"ok": True, "glb": f"export.{fmt}", "file": f"export.{fmt}", "fmt": fmt,
            "kb": round(dst.stat().st_size / 1024), "tris": tris,
            "engine": "Blender 4.2.3 LTS"}


def pipe_maya_export(src: Path, out_dir: Path, params: dict, progress) -> dict:
    """GLB → Maya 场景（.ma / .mb），走 mayapy standalone。

    注意：mayapy 的 Python 3.10 在中文路径下会把 argv 解释成乱码，
    所以必须把脚本与模型都搬到纯 ASCII 中转目录再跑。
    """
    import shutil
    import subprocess
    import tempfile
    fmt = str(params.get("fmt", "ma")).lower()
    if not MAYAPY_EXE.exists():
        raise RuntimeError(f"mayapy 不存在：{MAYAPY_EXE}")
    script = ROOT / "maya_ops.py"
    if not script.exists():
        raise RuntimeError("缺少 maya_ops.py")

    progress("准备中转目录（规避中文路径）", 12)
    # mayapy 的 argv 不认 UTF-8，必须用纯 ASCII 路径；8.3 短路径在本机被禁用，只能用 D:\sltmp
    tmpdir = Path(r"D:\sltmp\maya")
    tmpdir.mkdir(parents=True, exist_ok=True)
    src2 = tmpdir / ("in" + src.suffix)
    script2 = tmpdir / "maya_ops.py"
    shutil.copyfile(src, src2)
    shutil.copyfile(script, script2)
    dst2 = tmpdir / f"out.{fmt}"
    if dst2.exists():
        dst2.unlink()

    progress("启动 Maya", 25)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([str(MAYAPY_EXE), str(script2), str(src2), str(dst2), fmt],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600, env=env)
    out = (r.stdout or "") + (r.stderr or "")
    if not dst2.exists() or dst2.stat().st_size == 0:
        raise RuntimeError(f"Maya 导出失败：{out[-700:]}")

    final = out_dir / f"export.{fmt}"
    shutil.copyfile(dst2, final)
    tris = 0
    for line in out.splitlines():
        if line.startswith("MAYA_OK"):
            parts = line.split()
            for tok in parts[3:4]:
                if tok.isdigit():
                    tris = int(tok)
    progress("完成", 92)
    return {"ok": True, "glb": f"export.{fmt}", "file": f"export.{fmt}", "fmt": fmt,
            "kb": round(final.stat().st_size / 1024), "tris": tris,
            "engine": "Maya 2024 (mayapy)"}


# ─────────────────────────── worker 进程 ───────────────────────────
def worker_main(q_in, q_out):
    cache: dict = {}
    gpu = {"name": "CPU", "mem_gb": None, "cuda": False}
    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            gpu = {"name": props.name, "mem_gb": round(props.total_memory / 1024 ** 3, 1), "cuda": True}
            gpu["torch"] = torch.__version__
    except Exception:
        pass
    q_out.put({"type": "hello", "gpu": gpu, "pid": os.getpid()})

    while True:
        task = q_in.get()
        if task is None:
            break
        jid, pipe, args = task["job_id"], task["pipeline"], task["params"]
        src = Path(task["src"]) if task.get("src") else None
        srcs = [Path(x) for x in (task.get("srcs") or [])]
        out_dir = WORK / jid
        out_dir.mkdir(parents=True, exist_ok=True)
        args = dict(args)
        args["_cache"] = cache

        def progress(stage, pct, _jid=jid):
            q_out.put({"type": "progress", "job_id": _jid, "stage": stage, "progress": int(pct)})

        t0 = time.time()
        try:
            q_out.put({"type": "stage", "job_id": jid, "stage": "启动"})
            FN = {
                "depth": pipe_depth, "triposr": pipe_triposr, "simplify": pipe_simplify,
                "smooth": pipe_smooth, "repair": pipe_repair, "convert": pipe_convert,
                "render": pipe_render, "measure": pipe_measure,
                "multiview": pipe_multiview, "text2img": pipe_text2img,
                "text3d": pipe_text3d, "highpoly": pipe_highpoly, "uvbake": pipe_uvbake,
                "normalize": pipe_normalize, "assetcheck": pipe_assetcheck, "lod": pipe_lod,
                "blender_export": pipe_blender_export, "maya_export": pipe_maya_export,
            }
            if pipe not in FN:
                raise RuntimeError(f"未知管线 {pipe}")
            if pipe == "multiview":
                res = pipe_multiview(srcs, out_dir, args, progress)
            else:
                res = FN[pipe](src, out_dir, args, progress)
            res["ms"] = int((time.time() - t0) * 1000)
            # 自动缩略图
            try:
                target = res.get("glb") or res.get("file")
                if target and not (out_dir / "thumb.webp").exists():
                    cand = out_dir / target
                    if cand.exists() and cand.suffix.lower() in {".glb", ".obj", ".ply", ".stl", ".off", ".gltf", ".3mf"}:
                        try:
                            _render_png(str(cand), str(out_dir / "thumb.webp"), size=320)
                        except Exception:
                            _render_png(str(cand), str(out_dir / "thumb.png"), size=320)
            except Exception:
                pass
            q_out.put({"type": "done", "job_id": jid, "result": res})
        except Exception as e:  # noqa: BLE001
            q_out.put({"type": "error", "job_id": jid,
                       "error": f"{type(e).__name__}: {e}",
                       "trace": traceback.format_exc()[-2500:],
                       "ms": int((time.time() - t0) * 1000)})
        finally:
            # 每个任务后回收内存，避免长跑累积把 worker 撑爆
            try:
                import gc
                gc.collect()
                import torch as _t
                if _t.cuda.is_available():
                    _t.cuda.empty_cache()
            except Exception:
                pass


def _run_inline(jid: str, pipe: str, params: dict, src, srcs):
    """云模式：在当前进程的线程里执行管线，结束后释放内存。"""
    out_dir = WORK / jid
    out_dir.mkdir(parents=True, exist_ok=True)
    args = dict(params)
    args["_cache"] = {}
    src_p = Path(src) if src else None
    srcs_p = [Path(x) for x in (srcs or [])]

    def progress(stage, pct, _jid=jid):
        with LOCK:
            j = JOBS.get(_jid)
            if j:
                j["stage"] = stage
                j["progress"] = int(pct)
                _persist(j)

    FN = {
        "depth": pipe_depth, "triposr": pipe_triposr, "simplify": pipe_simplify,
        "smooth": pipe_smooth, "repair": pipe_repair, "convert": pipe_convert,
        "render": pipe_render, "measure": pipe_measure,
        "multiview": pipe_multiview, "text2img": pipe_text2img,
        "text3d": pipe_text3d, "highpoly": pipe_highpoly, "uvbake": pipe_uvbake,
        "normalize": pipe_normalize, "assetcheck": pipe_assetcheck, "lod": pipe_lod,
        "blender_export": pipe_blender_export, "maya_export": pipe_maya_export,
    }
    t0 = time.time()
    try:
        with LOCK:
            j = JOBS.get(jid)
            if j:
                j["stage"] = "计算中"
                j["progress"] = 5
        if pipe not in FN:
            raise RuntimeError(f"未知管线 {pipe}")
        if pipe == "multiview":
            res = pipe_multiview(srcs_p, out_dir, args, progress)
        else:
            res = FN[pipe](src_p, out_dir, args, progress)
        res["ms"] = int((time.time() - t0) * 1000)
        try:
            target = res.get("glb") or res.get("file")
            if target and not (out_dir / "thumb.webp").exists():
                cand = out_dir / target
                if cand.exists() and cand.suffix.lower() in {".glb", ".obj", ".ply", ".stl", ".off", ".gltf", ".3mf"}:
                    try:
                        _render_png(str(cand), str(out_dir / "thumb.webp"), size=320)
                    except Exception:
                        _render_png(str(cand), str(out_dir / "thumb.png"), size=320)
        except Exception:
            pass
        with LOCK:
            j = JOBS.get(jid)
            if j:
                j.update(status="done", progress=100, stage="完成", result=res,
                         finished_at=ts_short())
                _persist(j)
    except Exception as e:  # noqa: BLE001
        with LOCK:
            j = JOBS.get(jid)
            if j:
                j.update(status="failed", stage="失败", error=f"{type(e).__name__}: {e}",
                         trace=traceback.format_exc()[-2500:], finished_at=ts_short())
                _persist(j)
    finally:
        try:
            import gc
            gc.collect()
        except Exception:
            pass


def _ensure_engine():
    if CLOUD_MODE:
        # 云端不常驻 worker：省 700+ MB 内存，任务来了现开线程
        with LOCK:
            if not ENGINE.get("ready"):
                ENGINE.update(proc=None, q_in=None, q_out=None, ready=True,
                              gpu={"name": "CPU (cloud)", "mem_gb": None, "cuda": False},
                              started_at=ts_short())
        return
    with LOCK:
        if ENGINE["proc"] is not None and ENGINE["proc"].is_alive():
            return
        ctx = mp.get_context("spawn")
        q_in, q_out = ctx.Queue(), ctx.Queue()
        p = ctx.Process(target=worker_main, args=(q_in, q_out), daemon=True)
        p.start()
        ENGINE.update(proc=p, q_in=q_in, q_out=q_out, ready=False, started_at=ts_short())
    threading.Thread(target=_pump, args=(q_out,), daemon=True).start()


def _pump(q_out):
    while True:
        try:
            msg = q_out.get()
        except Exception:
            time.sleep(0.5)
            continue
        typ = msg.get("type")
        if typ == "hello":
            ENGINE["gpu"] = msg.get("gpu")
            ENGINE["ready"] = True
            continue
        jid = msg.get("job_id")
        if not jid:
            continue
        with LOCK:
            j = JOBS.get(jid)
            if j is None:
                continue
            if typ in ("progress", "stage"):
                j.update(stage=msg.get("stage", j.get("stage")), progress=msg.get("progress", j.get("progress")))
                j["updated_at"] = ts_short()
            elif typ == "done":
                r = msg["result"]
                j.update(status="done", stage="完成", progress=100, result=r,
                         ms=r.get("ms") or msg.get("ms"), finished_at=ts_short())
                j.pop("error", None)
            elif typ == "error":
                j.update(status="failed", stage="失败", error=msg.get("error"),
                         trace=msg.get("trace"), ms=msg.get("ms"), finished_at=ts_short())
            _persist(j)


def _persist(job: dict):
    try:
        d = WORK / job["job_id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


# ─────────────────────────── 路由 ───────────────────────────
@app.get("/api/pipelines")
def api_pipelines():
    return JSONResponse({"pipelines": PIPELINES})


@app.post("/api/run")
async def api_run(
    pipeline: str = Form(...),
    params: str = Form("{}"),
    image: Optional[UploadFile] = File(None),
    images: Optional[List[UploadFile]] = File(None),
    source: str = Form(""),
):
    pdef = PIPE_MAP.get(pipeline)
    if not pdef:
        raise HTTPException(400, f"未知管线 {pipeline}")
    if CLOUD_MODE and pdef["id"] in GPU_PIPES:
        raise HTTPException(400, "该管线需要 GPU，请在本地平台运行")
    try:
        pj = json.loads(params or "{}")
    except Exception:
        raise HTTPException(400, "params 不是合法 JSON")

    jid = uuid.uuid4().hex[:8]
    src_path = None
    src_paths: list[str] = []
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

    if pdef["needs"] == "image":
        if image is None:
            raise HTTPException(400, "该管线需要上传图片")
        ext = Path(image.filename or "x.jpg").suffix.lower()
        if ext not in allowed:
            raise HTTPException(400, "只支持 jpg / png / webp / bmp")
        up = UPLOADS / f"{jid}{ext}"
        up.write_bytes(await image.read())
        src_path = str(up)

    elif pdef["needs"] == "images":
        files = [f for f in (images or []) if f is not None]
        if not files:
            raise HTTPException(400, "该管线需要至少一张图片")
        if len(files) > 8:
            files = files[:8]
        for i, f in enumerate(files):
            ext = Path(f.filename or "x.jpg").suffix.lower()
            if ext not in allowed:
                raise HTTPException(400, f"第 {i+1} 张不是支持的图片格式")
            up = UPLOADS / f"{jid}_{i}{ext}"
            up.write_bytes(await f.read())
            src_paths.append(str(up))

    elif pdef["needs"] == "model":
        if not re.fullmatch(r"[a-f0-9]{8}", source or ""):
            raise HTTPException(400, "该管线需要选择一个已有模型")
        m = WORK / source / "model.glb"
        if not m.exists():
            cand = None
            d = WORK / source
            if d.exists():
                cand = next((p for p in d.glob("*.*") if p.suffix.lower()
                             in {".glb", ".obj", ".ply", ".stl", ".off", ".gltf", ".3mf"}), None)
            if cand is None:
                raise HTTPException(404, "模型不存在")
            m = cand
        src_path = str(m)

    job = {"job_id": jid, "pipeline": pipeline, "pipeline_name": pdef["name"],
           "status": "running", "progress": 0, "stage": "排队", "params": pj,
           "source": source or "", "views": len(src_paths) or None,
           "started_at": ts_short(), "created": now()}
    with LOCK:
        JOBS[jid] = job
    _persist(job)
    if CLOUD_MODE:
        threading.Thread(target=_run_inline,
                         args=(jid, pipeline, pj, src_path, src_paths), daemon=True).start()
    else:
        _ensure_engine()
        ENGINE["q_in"].put({"job_id": jid, "pipeline": pipeline, "params": pj,
                            "src": src_path, "srcs": src_paths})
    ENGINE["tasks"] += 1
    return JSONResponse({"ok": True, "job_id": jid})


@app.get("/api/job/{jid}")
def api_job(jid: str):
    if not re.fullmatch(r"[a-f0-9]{8}", jid):
        raise HTTPException(400, "id 不合法")
    with LOCK:
        j = JOBS.get(jid)
    if j is None:
        f = WORK / jid / "job.json"
        if f.exists():
            j = json.loads(f.read_text(encoding="utf-8"))
        else:
            raise HTTPException(404, "任务不存在")
    return JSONResponse(j)


@app.get("/api/jobs")
def api_jobs(limit: int = 30):
    items = []
    seen = set()
    with LOCK:
        for j in sorted(JOBS.values(), key=lambda x: x.get("started_at", ""), reverse=True):
            items.append(j)
            seen.add(j["job_id"])
    for d in sorted(WORK.iterdir(), key=lambda x: -x.stat().st_mtime) if WORK.exists() else []:
        if d.name in seen or not (d / "job.json").exists():
            continue
        try:
            items.append(json.loads((d / "job.json").read_text(encoding="utf-8")))
        except Exception:
            pass
        if len(items) > limit * 2:
            break
    return JSONResponse({"jobs": items[:limit]})


_MODELS_CACHE = {"at": 0.0, "data": None, "ttl": 20.0}


LIGHT_FIELDS = {"job_id", "asset_name", "tris", "kb", "thumb", "glb",
                "pipeline", "pipeline_name", "status", "mtime"}


@app.get("/api/models")
def api_models(refresh: bool = False, limit: int = 0, offset: int = 0, full: bool = False):
    """模型列表（20 秒内存缓存；默认精简字段并支持分页，减少公网传输量）。"""
    now = time.time()
    if (not refresh and _MODELS_CACHE["data"] is not None
            and now - _MODELS_CACHE["at"] < _MODELS_CACHE["ttl"]):
        return JSONResponse(_MODELS_CACHE["data"])
    out = []
    try:
        dirs = sorted(WORK.iterdir(), key=lambda x: -x.stat().st_mtime) if WORK.exists() else []
    except Exception:
        dirs = []
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            g = d / "model.glb"
            if not g.exists():
                g = next(iter(d.glob("converted.*")), None)
            if g is None or not Path(g).exists():
                continue
            st = Path(g).stat()
            info = {"job_id": d.name, "file": Path(g).name,
                    "kb": round(st.st_size / 1024),
                    "mtime": time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime)),
                    "glb": f"/files/{d.name}/{Path(g).name}",
                    "thumb": f"/thumb/{d.name}"}
            jf = d / "job.json"
            if jf.exists():
                try:
                    j = json.loads(jf.read_text(encoding="utf-8"))
                    info["pipeline"] = j.get("pipeline")
                    info["pipeline_name"] = j.get("pipeline_name")
                    info["asset_name"] = j.get("asset_name")
                    r = j.get("result") or {}
                    for k in ("tris", "verts", "size", "watertight", "volume", "area"):
                        if k in r:
                            info[k] = r[k]
                    info["status"] = j.get("status")
                except Exception:
                    pass
            out.append(info)
        except Exception:
            continue
    data = {"models": out}
    _MODELS_CACHE["data"] = data
    _MODELS_CACHE["at"] = now
    if full:
        return JSONResponse(data)

    # 精简字段（前端只需要这些），传输量约减半；支持分页
    raw = data.get("models") or []
    slim = [{k: v for k, v in m.items() if k in LIGHT_FIELDS} for m in raw]
    total = len(slim)
    start = max(0, int(offset or 0))
    end = total if not limit else min(total, start + int(limit))
    return JSONResponse({
        "models": slim[start:end],
        "count": total,
        "total_tris": data.get("total_tris") or sum(int(m.get("tris") or 0) for m in raw),
        "offset": start,
        "limit": int(limit or 0),
    })


@app.delete("/api/model/{jid}")
def api_model_delete(jid: str):
    if not re.fullmatch(r"[a-f0-9]{8}", jid):
        raise HTTPException(400, "id 不合法")
    d = WORK / jid
    if not d.exists():
        raise HTTPException(404, "模型不存在")
    shutil.rmtree(d, ignore_errors=True)
    with LOCK:
        JOBS.pop(jid, None)
    return JSONResponse({"ok": True})


def _dir_size(p: Path) -> int:
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ─────────────────────────── 混合云队列 ───────────────────────────
@app.post("/api/queue/submit")
async def api_queue_submit(
    pipeline: str = Form(...),
    params: str = Form("{}"),
    image: Optional[UploadFile] = File(None),
):
    """云端提交 GPU 任务，进入等待队列。"""
    if HQ is None:
        raise HTTPException(500, "队列未初始化")
    pdef = PIPE_MAP.get(pipeline)
    if not pdef:
        raise HTTPException(400, f"未知管线 {pipeline}")
    try:
        pj = json.loads(params or "{}")
    except Exception:
        raise HTTPException(400, "params 不是合法 JSON")
    data = None
    ext = ".png"
    if image is not None:
        ext = Path(image.filename or "x.png").suffix.lower() or ".png"
        data = await image.read()
    tid = HQ.submit(pipeline, pj, data, ext=ext, note=pdef.get("name", pipeline))
    return JSONResponse({"ok": True, "queued": True, "task_id": tid,
                         "tip": "已进入云端队列，本地 GPU 在线时会自动处理"})


@app.get("/api/queue/pending")
def api_queue_pending(token: str = "", worker: str = "local"):
    if HQ is None:
        raise HTTPException(500, "队列未初始化")
    if token != HQ.QUEUE_TOKEN:
        raise HTTPException(403, "token 不正确")
    t = HQ.claim_next(worker)
    if not t:
        return JSONResponse({"task": None})
    d = WORK / "_queue" / t["task_id"]
    out = dict(t)
    if t.get("input"):
        out["input_url"] = f"/api/queue/file/{t['task_id']}/{t['input']}"
    return JSONResponse({"task": out})


@app.get("/api/queue/file/{task_id}/{name}")
def api_queue_file(task_id: str, name: str):
    if HQ is None:
        raise HTTPException(500, "队列未初始化")
    if not re.fullmatch(r"[a-f0-9]{12}", task_id) or "/" in name or ".." in name:
        raise HTTPException(400, "路径不合法")
    p = WORK / "_queue" / task_id / name
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(p)


@app.post("/api/queue/result")
async def api_queue_result(
    token: str = Form(""),
    task_id: str = Form(...),
    result: str = Form("{}"),
    error: str = Form(""),
    glb: Optional[UploadFile] = File(None),
    thumb: Optional[UploadFile] = File(None),
):
    if HQ is None:
        raise HTTPException(500, "队列未初始化")
    if token != HQ.QUEUE_TOKEN:
        raise HTTPException(403, "token 不正确")
    try:
        rj = json.loads(result or "{}")
    except Exception:
        rj = {}
    files: dict[str, bytes] = {}
    if glb is not None:
        files["model.glb"] = await glb.read()
    if thumb is not None:
        files["thumb.webp"] = await thumb.read()
    ok = HQ.finish(task_id, result=rj, error=(error or None), files=files or None)
    return JSONResponse({"ok": ok})


@app.get("/api/queue/status/{task_id}")
def api_queue_status(task_id: str):
    if HQ is None:
        raise HTTPException(500, "队列未初始化")
    if not re.fullmatch(r"[a-f0-9]{12}", task_id):
        raise HTTPException(400, "id 不合法")
    t = HQ.load(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    out = dict(t)
    if (WORK / "_queue" / task_id / "model.glb").exists():
        out["glb_url"] = f"/api/queue/file/{task_id}/model.glb"
    if (WORK / "_queue" / task_id / "thumb.webp").exists():
        out["thumb_url"] = f"/api/queue/file/{task_id}/thumb.webp"
    return JSONResponse(out)


@app.get("/api/queue/workers")
def api_queue_workers():
    """算力节点在线状态。"""
    if HQ is None:
        return JSONResponse({"online": 0, "total": 0, "workers": {}})
    return JSONResponse(HQ.workers_status())


@app.get("/api/queue/stats")
def api_queue_stats():
    if HQ is None:
        return JSONResponse({"error": "队列未初始化"})
    return JSONResponse(HQ.stats())


@app.get("/api/status")
def api_status():
    _ensure_engine()
    with LOCK:
        jobs = len(JOBS)
        running = sum(1 for j in JOBS.values() if j.get("status") == "running")
    models = len([d for d in WORK.iterdir() if (d / "model.glb").exists()]) if WORK.exists() else 0
    ckpt = next((c for c in TRIPOSR_CANDIDATES if c.exists()), None)
    return JSONResponse({
        "ok": True,
        "mode": "cloud" if CLOUD_MODE else "local",
        "service": {"port": PORT, "uptime_since": ENGINE["started_at"], "now": ts_short()},
        "engine": {"alive": bool(ENGINE.get("ready")) or bool(ENGINE["proc"] and ENGINE["proc"].is_alive()),
                   "ready": ENGINE["ready"], "pid": ENGINE["proc"].pid if ENGINE["proc"] else None,
                   "gpu": ENGINE["gpu"], "tasks": ENGINE["tasks"]},
        "jobs": {"total": jobs, "running": running},
        "models": models,
        "storage": {"work_mb": round(_dir_size(WORK) / 1024 / 1024, 1),
                    "uploads_mb": round(_dir_size(UPLOADS) / 1024 / 1024, 1)},
        "triposr": {"weights": str(ckpt) if ckpt else None,
                    "weights_mb": round(ckpt.stat().st_size / 1024 / 1024, 1) if ckpt else None,
                    "config": TRIPOSR_CFG.exists()},
        "pipelines": [{"id": p["id"], "name": p["name"], "group": p["group"]} for p in PIPELINES],
    })


@app.get("/files/{jid}/{name}")
def api_file(jid: str, name: str):
    if not re.fullmatch(r"[a-f0-9]{8}", jid) or "/" in name or ".." in name or "\\" in name:
        raise HTTPException(400, "路径不合法")
    p = WORK / jid / name
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(p)


@app.get("/thumb/{jid}")
@app.get("/thumb/{jid}.webp")
@app.get("/thumb/{jid}.png")
def api_thumb(jid: str):
    if not re.fullmatch(r"[a-f0-9]{8}", jid):
        raise HTTPException(400, "id 不合法")
    d = WORK / jid
    for name, mime in (("thumb.webp", "image/webp"), ("thumb.png", "image/png")):
        p = d / name
        if p.exists():
            return FileResponse(p, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})
    raise HTTPException(404, "没有缩略图")


def _page(name: str):
    p = STATIC / name
    if not p.exists():
        raise HTTPException(404, f"缺少 static/{name}")
    return HTMLResponse(p.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
def index():
    """首页：注入本次启动的版本号，避免浏览器拿旧缓存。"""
    p = STATIC / "index.html"
    if not p.exists():
        raise HTTPException(404, "缺少 static/index.html")
    return HTMLResponse(p.read_text(encoding="utf-8").replace("__V__", BOOT_TAG))


@app.get("/viewer", response_class=HTMLResponse)
def viewer():
    if (STATIC / "viewer.html").exists():
        return _page("viewer.html")
    return _page("index.html")


@app.get("/sw.js")
def service_worker():
    """Service Worker 必须在根路径，否则 scope 会被限制在 /static/。"""
    p = STATIC / "sw.js"
    if not p.exists():
        raise HTTPException(404, "缺少 static/sw.js")
    return FileResponse(p, media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "now": ts_short()})


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _lan_ips() -> list[str]:
    """列出本机局域网 IPv4，供手机/平板直连。"""
    out = []
    try:
        import socket
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in out:
                out.append(ip)
    except Exception:
        pass
    if not out:
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            out.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return out


def main() -> int:
    url = f"http://127.0.0.1:{PORT}/"
    print("=" * 58)
    print("  Wildfire 2.0 · 3D 生产平台")
    print(f"  本机：{url}")
    for ip in _lan_ips():
        print(f"  局域网（同一 WiFi 手机/平板可开）：http://{ip}:{PORT}/")
    print(f"  管线：{len(PIPELINES)} 条")
    print("=" * 58)
    import uvicorn
    if OPEN_BROWSER:
        threading.Timer(1.5, lambda: __import__("webbrowser").open(url)).start()
    # 0.0.0.0：本机 + 局域网都能访问；首次运行 Windows 会弹防火墙授权
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
