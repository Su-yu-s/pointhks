from collections import OrderedDict
from pathlib import Path

import numpy as np
import torch

from datasets.transform import Compose
from litept.model import LitePT

if __name__ == "__main__":

    weights_dir = Path(__file__).resolve().parent / "weights"
    ckpt_path = weights_dir / "model_best.pth"
    lidar_path = weights_dir / "outdoor_sample1.bin"
    if not ckpt_path.is_file() or not lidar_path.is_file():
        raise FileNotFoundError(
            "Place model_best.pth and outdoor_sample1.bin in "
            f"{weights_dir} before running this demo."
        )

    ### Step 1. create model
    ### we use the default config of the model, which is litePT-S. 
    ### adjust the config inside the model according to your need.
    model = LitePT()
    model.cuda()

    ### optional, load pretrained weights
    ### e.g. we load pretrained weights on NuScenes semantic segmentation
    ckpt = torch.load(ckpt_path, map_location="cpu")
    weight = OrderedDict()
    prefix = "module.backbone."
    for key, value in ckpt["state_dict"].items():
        if key.startswith(prefix):
            new_key = key[len(prefix):]
            weight[new_key] = value
    model.load_state_dict(weight, strict=True)
    model.eval()

    ### Step 2. prepare data
    ### we take an example scene from NuScenes, the input is a N×4 array [x,y,z,strength]
    points = np.fromfile(lidar_path, dtype=np.float32, count=-1).reshape(
            [-1, 5]
        )
    coord = points[:, :3] # [N, 3]
    strength = points[:, 3].reshape([-1, 1]) / 255  # scale strength to [0, 1]
    point = dict(
        coord=coord,
        strength=strength,
        )
    print("Number of points: ", coord.shape[0])
    
    ### apply basic transforms
    data_config = [
        dict(
            type="GridSample",
            grid_size=0.05,
            hash_type="fnv",
            mode="train",
            return_grid_coord=True,
            return_inverse=True,
        ),
        dict(type="ToTensor"),
        dict(
            type="Collect",
            keys=("coord", "grid_coord", "inverse"),
            feat_keys=("coord", "strength"),
        ),
    ]
    transform = Compose(data_config)
    point = transform(point)

    ### Step 3. forward the model
    with torch.no_grad():
        for key in point.keys():
            if isinstance(point[key], torch.Tensor):
                point[key] = point[key].cuda(non_blocking=True)
        # forward
        point = model(point) # [N_down, C]
        # point is downsampled by GridSample in transform
        print("Downsampled output feature: ", point.feat.shape)
        # obtain per-point feature
        dense_feat = point.feat[point.inverse] # [N, C]
        print("Dense output feature: ", dense_feat.shape)

    ### Done! That's how to use LitePT to encode a point cloud.
