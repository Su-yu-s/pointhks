# ScanNet200 实例分割第一阶段实施计划

1. 编译并导入 `pointgroup_ops`，使用项目内 sparsehash 头文件。
2. 调整 PointGroup 的评估前向，使无真值标签的自定义点云可以推理，同时保留训练行为。
3. 新增 `tools/infer_scannet200_instances.py`：读取 PLY/PCD、检查 RGB、估计缺失法向、体素采样、推理和结果映射。
4. 输出 `labeled_cloud.ply`、`point_labels.bin`、`instances.json` 和 `run.json`。
5. 添加无需 GPU 的读取器/导出器自动测试，并执行 checkpoint 严格加载测试。
6. 使用合成 RGB 房间点云完成 GPU 冒烟测试；该测试只验证链路，不评价模型精度。
7. 找到或由用户提供真实 RGB 室内点云后，完成质量验收，再进入 Qt/C++ 集成。
