# LitePT 稳定推理系统

当前系统只负责 LitePT 推理，不包含 Qt、RANSAC 或尺寸测量。统一入口是 `tools/litept_infer.py`，输出协议版本为 `schema_version: 1`。

## 1. 模型配置

查看可用模型：

```bash
cd /mnt/e/C++/Point/LitePT
source /opt/litept-venv/bin/activate
python tools/litept_infer.py --list-profiles
```

内置配置：

- `scannet200-instances`：ScanNet200 室内实例分割，输入为带 XYZ+RGB 的 PLY/PCD，输出语义和实例。
- `nuscenes-semantic`：NuScenes 室外语义分割，输入为 float32 `N×5` BIN，只输出逐点语义，不输出实例。

NuScenes 权重不能用于室内实例轮廓，ScanNet200 权重也不应解释为精确施工测量模型。

自定义权重通过外部 JSON 配置接入。文件结构与 `configs/inference_profiles.json` 相同；路径相对该 JSON 所在目录解析，入口脚本必须接受位置参数 `input` 以及 `--config`、`--weight`、`--output-dir`。运行时增加：

```bash
python tools/litept_infer.py cloud.ply \
  --profiles-file /path/to/custom_profiles.json \
  --profile my-model \
  --output-dir outputs/my-model-result
```

## 2. 推理前检查

检查文件格式、字段、RGB、法向、无效点、单位、坐标范围和上方向，不加载显卡模型：

```bash
python tools/litept_infer.py /path/to/cloud.ply \
  --profile scannet200-instances \
  --check-input \
  --input-unit m
```

室内实例模型要求真实 RGB。只有 XYZ 的 PCD 会明确拒绝，不会自动填充伪颜色。PCD 支持 `ascii`、`binary` 和 `binary_compressed`。厘米或毫米输入分别用 `--input-unit cm`、`--input-unit mm`；NuScenes BIN 固定使用米。

## 3. 单文件推理

室内实例分割：

```bash
python tools/litept_infer.py /path/to/cloud.ply \
  --profile scannet200-instances \
  --output-dir outputs/my-indoor-cloud
```

NuScenes 语义分割：

```bash
python tools/litept_infer.py weights/outdoor_sample1.bin \
  --profile nuscenes-semantic \
  --output-dir outputs/my-nuscenes-cloud
```

输出目录必须为空。确实要替换该目录中的 LitePT 产物时使用 `--overwrite-output`；脚本只清理已知 LitePT 文件和 `instances/`，不会删除其他文件。失败默认输出简短错误；开发调试可增加 `--debug` 查看堆栈。

## 4. 大点云和实例后处理

ScanNet200 输入超过 50 万有效点时自动分块。默认会缩小核心瓦片，直到单块不超过 75 万原始点；整云显存不足时也会自动切换分块。常用参数：

- `--tiling auto|on|off`
- `--tile-threshold 500000`
- `--tile-size 6.0`
- `--tile-overlap 0.75`
- `--max-tile-points 750000`
- `--min-tile-size 1.0`
- `--oom-retries 2`
- `--tile-merge-ratio 0.5`
- `--tile-merge-iou 0.25`
- `--tile-min-shared-points 20`

跨块融合先按最高语义置信度回写每个原始点，再利用重叠区中的共享原始点合并同类别实例。

实例后处理默认开启：8 cm 连通体素用于分离断开部分，删除少于 20 点且不足原实例 1% 的孤立簇，只合并同类别且实际点间距不超过 4 cm 的实例。参数为 `--component-voxel-size`、`--component-min-points`、`--component-min-ratio`、`--instance-merge-distance`；使用 `--no-postprocess` 可关闭。

## 5. 输出协议与验收

- `request.json`：完整请求参数。
- `run.json`：状态、模型、点数、耗时、显存、分块及后处理统计。
- `point_labels.bin`：每个原始点 12 字节，little-endian `int32 semantic_id`、`int32 instance_id`、`float32 confidence`。
- `source_index.bin`：每个有效点一个 little-endian `int64` 原始索引。
- `labeled_cloud.ply`：有效点、颜色、标签、置信度和原始索引。
- `instances.json`、`instances/*.ply`：仅实例分割任务生成。

无效原始点固定写入 `-1/-1/0`。JSON、BIN 和 PLY 使用同目录临时文件完成后原子替换。进度在标准错误中按 `LITEPT_EVENT {JSON}` 输出，每条事件也带 `schema_version: 1`。

自动检查输出：

```bash
python tools/verify_inference_output.py outputs/my-indoor-cloud
```

## 6. 批量、性能与精度

批量清单示例为 `configs/batch_manifest.example.json`：

```bash
python tools/batch_infer.py configs/batch_manifest.example.json --continue-on-error
```

每个任务结束后自动执行输出完整性检查，并生成 `*_report.json`。

性能测试：

```bash
python tools/benchmark_inference.py weights/outdoor_sample1.bin \
  --profile nuscenes-semantic \
  --output-root outputs/benchmark-nuscenes \
  --warmup 1 --repeat 3
```

报告分别记录模型推理、完整流水线和进程墙钟时间，以及吞吐量和峰值显存。

有逐点真值时，真值也保存为同一 12 字节标签协议，然后运行：

```bash
python tools/evaluate_labels.py \
  outputs/result/point_labels.bin \
  ground_truth/point_labels.bin \
  --instance-iou 0.5 \
  --output outputs/result/metrics.json
```

输出语义总体准确率、各类 IoU、mIoU，以及类别一致的实例 IoU 匹配、精确率、召回率和 F1。没有人工真值时只能做定性观察，不能宣称精度。

## 7. 已验证结果

- NuScenes 样例：34,688 点全部回写，模型推理约 0.8–1.0 秒，峰值显存约 249 MiB。
- 办公室样例：276,871 个原始点，312 个无效点正确映射；后处理得到 8 个实例。
- 大型客厅样例：888,100 个原始点，自动从 6 m 缩小到 1.5 m 核心瓦片并完成 9 块融合；888,100 条标签全部输出。
- 百万级压力样例：合并两个已对齐 LivingRoom 片段得到 1,084,233 个原始点，其中 1,072,178 点有效；自动依次尝试 6/3/1.5/1.0 m，最终完成 16 块推理、融合和后处理，输出 39 个实例，完整流程约 11.1 秒，峰值显存约 348 MiB，全部产物通过一致性检查。该合成输入只用于容量测试，不作为精度样例。
- RANSAC 项目的自有 `result_colored.ply`：485,310 点，按毫米转换后 2 cm 网格实际只剩 168 个模型输入点，输出 0 个实例，语义大面积误判为 `office chair/window/ceiling`。该文件来自几何模型并使用人工着色，不是 ScanNet 风格的带真实 RGB 室内扫描，结果证明当前权重不适用于这类小尺度机械/工装数据。
- 自动测试覆盖格式读取、压缩 PCD、无效点、坐标方向、分块、跨块融合、显存错误识别、后处理、模型配置、批量清单与指标计算。

当前现有权重的主要限制是领域差异。ScanNet200 可做与 ScanNet 接近的室内对象候选分割，但对项目特有构件、材料、施工场景以及小尺度机械件不可靠。根据自有几何数据的实测，如果这些对象属于目标范围，应训练专用模型；建筑/装修方向还应另采集带真实 RGB 的自有场景并制作人工真值，用同一评估工具确认类别设计和精度目标。
