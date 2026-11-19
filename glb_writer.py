# -*- coding: utf-8 -*-
"""精简 GLB / OBJ 写出器：不经过 trimesh 的贴图深拷贝，内存可控。

只支持单一网格 + 单张贴图（UV0 + baseColor），够游戏资产用。
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np


def write_glb(path: str, vertices: np.ndarray, faces: np.ndarray,
              uvs: np.ndarray | None = None, png_bytes: bytes | None = None,
              metallic: float = 0.0, roughness: float = 0.75) -> str:
    """顶点位置/索引/UV/贴图 → 标准 GLB。"""
    v = np.ascontiguousarray(vertices, dtype=np.float32)
    f = np.ascontiguousarray(faces, dtype=np.uint32).reshape(-1)
    uv = np.ascontiguousarray(uvs, dtype=np.float32) if uvs is not None else None

    blobs: list[bytes] = []
    views: list[dict] = []
    accessors: list[dict] = []
    offset = 0

    def add_view(data: bytes, target: int | None = None) -> int:
        nonlocal offset
        # 4 字节对齐
        pad = (-offset) % 4
        if pad:
            blobs.append(b"\x00" * pad)
            offset += pad
        idx = len(views)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        views.append(view)
        blobs.append(data)
        offset += len(data)
        return idx

    # POSITION
    pos_view = add_view(v.tobytes(), 34962)
    accessors.append({
        "bufferView": pos_view, "componentType": 5126, "count": int(v.shape[0]),
        "type": "VEC3",
        "min": [float(x) for x in v.min(axis=0)],
        "max": [float(x) for x in v.max(axis=0)],
    })
    attributes = {"POSITION": 0}

    if uv is not None:
        uv_view = add_view(uv.tobytes(), 34962)
        accessors.append({"bufferView": uv_view, "componentType": 5126,
                          "count": int(uv.shape[0]), "type": "VEC2"})
        attributes["TEXCOORD_0"] = len(accessors) - 1

    idx_view = add_view(f.tobytes(), 34963)
    accessors.append({"bufferView": idx_view, "componentType": 5125,
                      "count": int(f.size), "type": "SCALAR"})
    idx_accessor = len(accessors) - 1

    material: dict = {
        "pbrMetallicRoughness": {
            "metallicFactor": float(metallic),
            "roughnessFactor": float(roughness),
        },
        "doubleSided": True,
    }
    textures = []
    images = []
    samplers = []
    if png_bytes:
        img_view = add_view(png_bytes)
        images = [{"bufferView": img_view, "mimeType": "image/png"}]
        samplers = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}]
        textures = [{"source": 0, "sampler": 0}]
        material["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}

    # 合并 BIN
    bin_blob = b"".join(blobs)
    bin_pad = (-len(bin_blob)) % 4
    if bin_pad:
        bin_blob += b"\x00" * bin_pad

    gltf = {
        "asset": {"version": "2.0", "generator": "SpatialLab"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "model"}],
        "meshes": [{"primitives": [{
            "attributes": attributes, "indices": idx_accessor, "material": 0,
        }]}],
        "materials": [material],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(bin_blob)}],
    }
    if textures:
        gltf["textures"] = textures
        gltf["images"] = images
        gltf["samplers"] = samplers

    json_chunk = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_pad = (-len(json_chunk)) % 4
    json_chunk += b" " * json_pad

    total = 12 + 8 + len(json_chunk) + 8 + len(bin_blob)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, total))
        fh.write(struct.pack("<II", len(json_chunk), 0x4E4F534A))
        fh.write(json_chunk)
        fh.write(struct.pack("<II", len(bin_blob), 0x004E4942))
        fh.write(bin_blob)
    return path


def write_obj(path: str, vertices: np.ndarray, faces: np.ndarray,
              uvs: np.ndarray | None = None, tex_name: str | None = None) -> str:
    """写 OBJ + MTL（带 vt 与贴图引用）。"""
    p = Path(path)
    mtl_name = p.stem + ".mtl"
    v = np.asarray(vertices, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    lines = ["# SpatialLab export", f"mtllib {mtl_name}"]
    for x, y, z in v:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    has_uv = uvs is not None
    if has_uv:
        for u, w in np.asarray(uvs, dtype=np.float64):
            lines.append(f"vt {u:.6f} {w:.6f}")
    lines.append("usemtl material0")
    for tri in f:
        a, b, c = tri + 1
        if has_uv:
            lines.append(f"f {a}/{a} {b}/{b} {c}/{c}")
        else:
            lines.append(f"f {a} {b} {c}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    mtl = ["# SpatialLab material", "newmtl material0", "Kd 1.0 1.0 1.0",
           "Ka 0.0 0.0 0.0", "Ks 0.0 0.0 0.0", "d 1.0", "illum 2"]
    if tex_name:
        mtl += [f"map_Kd {tex_name}"]
    (p.parent / mtl_name).write_text("\n".join(mtl) + "\n", encoding="utf-8")
    return str(p)
