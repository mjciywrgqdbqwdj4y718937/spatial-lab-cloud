# Spatial Lab · Cloud Edition

同一套代码，云端模式（`CLOUD_MODE=1`）自动隐藏需要 GPU 的管线，其余全部可用。

## 部署

```bash
cd /opt/lab
git clone https://github.com/mjciywrgqdbqwdj4y718937/spatial-lab-cloud.git
cd spatial-lab-cloud
pip3 install -r requirements.txt
CLOUD_MODE=1 nohup python3 server2.py 8091 --no-browser > /opt/lab/platform.log 2>&1 &
```

## 管线可用性

| 云端可用 | 需要 GPU（本地运行） |
|---|---|
| 格式转换 convert | 单图重建 depth |
| 几何测量 measure | AI 生成 triposr |
| 渲染出图 render | 文生 3D text3d |
| 游戏就绪体检 assetcheck | 文生图 text2img |
| 多级 LOD lod | 多视角融合 multiview |
| 简化 simplify | 高模细分 highpoly |
| 平滑 smooth | |
| 修复 repair | |
| UV 展开 uvbake | |

## 素材库种子

```bash
cd /opt/lab/spatial-lab-library && python3 ../spatial-lab-cloud/cloud_seed.py
```

把静态站里精选的模型映射成 `work/` 目录结构，平台模型库就能直接看到。
