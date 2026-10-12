# LitePT 实验环境归档

原目录：`E:\C++\Point\CloudForge-Neural`（已于归档时删除，释放约 2.4 GB）

## 这是什么

2026-09 期间团队的点云**神经网络分割**探索环境，基于第三方开源模型
**LitePT**（ETH Zurich，CVPR 2026，arXiv:2512.13689，MIT）。
源目录是解压的源码副本，不含 `.git`，因此按「文件修改时间晚于源码落盘时间」
识别自研内容并归档。

## 为什么被删除

2026-10-03 技术边界决策：**主线不接入 LitePT**（见 CloudForge
`hackathon/PLAN.md` §技术边界决策）。理由：

1. 合规零争议——作品 = Agnes 模型（决策）+ 纯本地算法（几何计算），
   完全规避「仅限 Agnes 模型」的解释成本；
2. 评委复现简单——无需 WSL / Python / 模型权重，也无 ScanNet 训练数据
   带来的权重开源许可问题；
3. 本科组技术深度仅占 10%，RANSAC 核心已足够支撑；
4. 省下 1–2 天投给视频、轨迹留证、文档（直接对分）。

主线路径：qRANSAC_SD 检测 → CadQuery 出 CAD → FreeCAD 验证，纯几何算法。

## 归档了什么

自研胶水层 + 设计与测试记录（这些无法从 HuggingFace 或 GitHub 重建）：

| 路径 | 内容 |
|---|---|
| `tools/cloudforge_profiles.py` | 推理档位加载器（schema 校验、相对/绝对路径解析） |
| `tools/cloudforge_infer.py` | 推理入口 |
| `tools/cloudforge_check_input.py` | 输入点云字段校验 |
| `tools/verify_inference_output.py` | 推理产物校验 |
| `configs/inference_profiles.json` | 5 个档位：ScanNet200/实例、ScanNet/实例、ScanNet/语义、Structured3D/语义、NuScenes/语义 |
| `configs/batch_manifest.example*.json` | 批处理清单示例 |
| `docs/scannet200-instance-inference.md` | ScanNet200 实例分割推理文档 |
| `docs/stable-inference.md` | 稳定推理文档 |
| `docs/superpowers/plans/`、`specs/` | ScanNet200 实例分割流水线 plan 与设计 spec |
| `tests/test_inference_tooling.py` | 推理工具测试 |
| `tests/test_scannet200_instance_io.py` | ScanNet200 实例 IO 测试 |
| `models/point_group/point_group_v1m2_custom_criteria.py` | PointGroup 自定义判据 |
| `litept-cluster-20260908/` | 集群化设计稿（HTML + 预览图） |
| `tools/`、`configs/`、`models/` 其余 | LitePT 上游源码，一并留存备查 |

已确认跑通的产物：对客厅点云做实例分割，切出 ceiling / window / cabinet /
picture / printer / monitor / desk / chair / keyboard / computer_tower /
trash_can 等逐实例 PLY + `labeled_cloud.ply`（2026-09-19 最后一次运行）。

## 没有归档什么（均可重建）

| 内容 | 体积 | 重建方式 |
|---|---|---|
| `weights/` | 805 MB | HuggingFace `prs-eth/LitePT` 下载 |
| `.venv/` | 685 MB | `conda create -n litept python=3.10` + `pip install -r requirements.txt` |
| `outputs/` | 804 MB | 重跑推理 |
| `libs/*/build/` | 136 MB | WSL 下 `python setup.py install` 重新编译 CUDA 算子 |
| `.idea/`、`.superpowers/*/state/` | — | IDE 与临时状态，无价值 |

## 合规提醒

本目录**不属于参赛作品**，也不得进入参赛包。CloudForge 的
`hackathon/declaration.md` §四已如实声明 LitePT 为第三方开源模型、非本队自研、
不接入执行链路、不进演示；`hackathon/pack-submission.py` 会在打包后自动校验
包内不含任何 `litept` 字样的文件路径。

本归档位于 CloudForge 仓库之外，天然不会被打包。