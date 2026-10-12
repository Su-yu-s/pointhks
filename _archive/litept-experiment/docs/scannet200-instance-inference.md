# ScanNet200 实例分割使用说明

## 输入要求

- 文件格式：PLY 或 PCD。
- 必须包含 XYZ 和真实 RGB。
- 法向量字段可选：PLY 支持 `nx/ny/nz` 或 `normal_x/normal_y/normal_z`；PCD 规则相同。
- PLY 如果包含三角网格面，会按 ScanNet 预处理方式计算顶点法向。
- 纯点云缺少法向时，脚本使用 KNN 局部平面估计。
- PCD 支持 `DATA ascii`、`DATA binary` 和 `DATA binary_compressed`。
- 默认坐标单位为米；厘米或毫米输入使用 `--input-unit cm` 或 `--input-unit mm`。
- 默认 `--up-axis auto`，会检测 X/Y/Z 上方向并统一转换为 Z-up；也可用 `--up-axis z` 或 `--up-axis -y` 明确覆盖。
- 默认自动丢弃 NaN/Inf，并通过原始索引映射写回标签；调试时可用 `--invalid-policy error` 改为立即报错。

## 运行命令

```bash
wsl -d Ubuntu
cd /mnt/e/C++/Point/LitePT
source /opt/litept-venv/bin/activate
python tools/infer_scannet200_instances.py \
  data/samples/scan2cad_scene0470_00/scene0470_00_vh_clean_2.ply \
  --output-dir outputs/my_result
```

处理自己的点云：

```bash
python tools/infer_scannet200_instances.py /mnt/e/path/to/cloud.ply \
  --output-dir outputs/my_cloud \
  --input-unit m
```

超过 50 万个有效点时，默认 `--tiling auto` 会自动执行带重叠区域的 XY 分块推理，并根据重叠区域中共享的原始点合并跨块实例。可以显式控制：

```bash
python tools/infer_scannet200_instances.py /mnt/e/path/to/large_cloud.ply \
  --output-dir outputs/large_cloud \
  --tiling on \
  --tile-size 6.0 \
  --tile-overlap 0.75
```

常用分块参数：

- `--tiling auto|off|on`：自动、禁用或强制分块，默认 `auto`。
- `--tile-threshold 500000`：自动分块的有效点数阈值。
- `--tile-size 6.0`：每块核心区域的 XY 边长，单位米。
- `--tile-overlap 0.75`：相邻块重叠宽度，单位米。
- `--tile-merge-ratio 0.5`：共享归属点相对较小实例的合并比例。
- `--tile-merge-iou 0.25`：重叠区域实例 IoU 合并阈值。
- `--tile-min-shared-points 20`：允许跨块合并所需的最少共享点数。
- `--min-instance-points 50`：最终保留实例的最少原始点数。
- `--max-tile-points 750000`：如果单块过密，自动缩小瓦片直到不超过该点数。
- `--min-tile-size 1.0`：自适应缩小允许的最小核心瓦片尺寸。
- `--oom-retries 2`：分块显存不足时继续缩小瓦片的重试次数。

默认还会执行实例连通域清理、拆分和保守合并，详情及统一入口参见 `docs/stable-inference.md`。

分块过程会向标准错误输出以 `LITEPT_EVENT ` 开头的单行 JSON 进度事件，调用程序可以据此显示法向估计、坐标判断、模型推理、瓦片融合和导出进度。

## 输出文件

- `request.json`：本次实际输入、模型、预处理和分块参数。
- `labeled_cloud.ply`：全部有效点的 Z-up 点云，按实例着色，并附带 `source_index`、语义编号、实例编号和置信度。
- `point_labels.bin`：每个原始点 12 字节，依次为 little-endian `int32 semantic_id`、`int32 instance_id`、`float32 confidence`；无效点写为 `-1/-1/0`。
- `source_index.bin`：每个有效点一个 little-endian `int64` 原始点索引。
- `instances/instance_NNN_class.ply`：每个识别实例的独立原色点云，可直接交给 RANSAC。
- `instances.json`：每个实例的类别、点数、置信度、米制轴对齐包围盒和独立 PLY 路径。
- `run.json`：本次运行的输入、模型、点数、语义统计、耗时和峰值显存。

`request.json`、`instances.json` 和 `run.json` 当前使用 `schema_version: 1`。JSON、PLY 和 BIN 先写入同目录临时文件，写完后再原子替换目标文件，避免中断时留下半个结果文件。

`run.json` 的 `tiling` 字段会记录是否实际启用分块、瓦片数量、尺寸、重叠宽度和融合阈值。分块模式中的 `sampled_points` 是所有瓦片采样点数之和，包含重叠区域的重复处理量。

## 已验证样例

- `cloud_bin_0.ply`：276,871 个原始点，默认整云推理，输出 7 个实例，标签记录数与原始点数一致。
- `livingroom1-fragments-ply/cloud_bin_48.ply`：888,100 个原始点，`auto` 自动启用 4 个瓦片，过滤 12,055 个无效点，融合后输出 11 个实例；标签记录 888,100 条，索引映射 876,045 条。

## 当前限制

- ScanNet200 是通用室内模型，不保证识别项目特有构件或材料。
- 神经网络结果用于筛选对象，不能直接作为施工尺寸。
- 尺寸需要对选中实例继续执行 RANSAC、轮廓或包围盒测量。
- 分块推理已实现，但瓦片尺寸和重叠宽度仍需根据实际点密度、对象尺寸和显存进行验证；过小瓦片会减少模型上下文并影响识别类别。
- 当前跨块实例仅在相邻瓦片的重叠区域共享足够多原始点且语义类别相同时合并；遮挡严重或两块预测类别不一致时，实例仍可能被拆开。
- 自动上方向依赖场景中的水平支撑面；极小的单一墙面或无明显水平结构时，应使用 `--up-axis` 人工覆盖，并检查 `run.json` 中的判断分数。
