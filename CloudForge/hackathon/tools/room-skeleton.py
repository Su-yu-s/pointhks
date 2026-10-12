# 房间骨架重建：竖直墙取水平投影凸包、从地面拉伸到顶棚（全高），加地板/顶棚
# 这是建筑平面图式的重建——墙立起来、顶天立地，而不是悬空的平面碎片
# 用法: python hackathon/tools/room-skeleton.py <result.json> [点云.ply] <out.step> [min_inlier]
#   点云路径可省略：新版 detect 的 result.json 含 source_cloud 字段时自动读取
import json, math, os, struct, sys
import numpy as np
from scipy.spatial import ConvexHull
import cadquery as cq

RES = sys.argv[1]
REST = sys.argv[2:]
PLY = None
if REST and os.path.splitext(REST[0])[1].lower() in (".ply", ".pcd"):
    PLY, OUT, MIN = REST[0], REST[1], REST[2] if len(REST) > 2 else "800"
else:
    OUT, MIN = REST[0], REST[1] if len(REST) > 1 else "800"
MIN_INLIER = int(MIN)
THICK = 0.06

d = json.load(open(RES))
if PLY is None:
    PLY = d.get("source_cloud")
    if not PLY or not os.path.exists(PLY):
        raise SystemExit("point cloud not given and result.json has no readable source_cloud")
print("source cloud:", PLY)

raw = open(PLY, "rb").read()
hend = raw.find(b"end_header")
stride, vert_count, in_v = 0, 0, False
for line in raw[:hend].decode("ascii", "ignore").splitlines():
    t = line.strip().split()
    if not t:
        continue
    if t[0] == "element":
        in_v = t[1] == "vertex"
        if in_v:
            vert_count = int(t[2])
    elif t[0] == "property" and in_v:
        stride += {"float": 4, "float32": 4, "double": 8, "uchar": 1, "uint8": 1, "int": 4, "uint": 4}[t[1]]
data_start = raw.find(b"\n", hend) + 1

def read_xyz(indices):
    out = np.empty((len(indices), 3))
    for k, i in enumerate(indices):
        out[k] = struct.unpack_from("<fff", raw, data_start + i * stride)
    return out

d = json.load(open(RES))
walls, horizontals = [], []
for s in d["shapes"]:
    if s["inlier_count"] < MIN_INLIER or s["type"] != "Plane":
        continue
    nx, ny, nz, dd = s["coefficients"]
    nlen = math.sqrt(nx*nx + ny*ny + nz*nz)
    nz_n = nz / nlen
    idx = [int(x) for x in open(os.path.join(os.path.dirname(RES), "indices", "shape_%d.txt" % s["id"])).read().split()]
    pts = read_xyz(idx)
    zc = float(pts[:, 2].mean())
    if abs(nz_n) < 0.5:
        walls.append((s["id"], pts))
    else:
        horizontals.append((s["id"], zc, pts, nz_n))

if not horizontals:
    raise SystemExit("no horizontal planes found")
horizontals.sort(key=lambda t: t[1])
floor = horizontals[0]
ceil = max(horizontals, key=lambda t: t[1])
z0, z1 = float(floor[2][:, 2].min()), float(ceil[2][:, 2].max())
print("floor z=%.3f (id=%d), ceil z=%.3f (id=%d), %d walls" % (z0, floor[0], z1, ceil[0], len(walls)))

def hull2d(pts):
    return pts[ConvexHull(pts).vertices]

solids = []
# 竖直墙：水平投影凸包 → 从地面拉伸到顶棚
for wid, pts in walls:
    hull = hull2d(pts[:, :2])
    area = 0.5 * abs(np.dot(hull[:, 0], np.roll(hull[:, 1], -1)) - np.dot(hull[:, 1], np.roll(hull[:, 0], -1)))
    if area < 0.02:
        continue
    wp = cq.Workplane("XY", origin=(0, 0, z0))
    solid = wp.polyline([tuple(p) for p in hull]).close().extrude(z1 - z0).val()
    solids.append(("wall#%d" % wid, solid))
    print("  wall id=%d: footprint area %.2f m2, %d vtx" % (wid, area, len(hull)))

# 地板/顶棚：各自平面内凸包，水平拉伸厚度
for label, h in (("floor", floor), ("ceil", ceil)):
    sid, zc, pts, nz_n = h
    # 投影到水平面用 x/y 凸包，厚度沿 z
    hull = hull2d(pts[:, :2])
    wp = cq.Workplane("XY", origin=(0, 0, zc - THICK / 2))
    solid = wp.polyline([tuple(p) for p in hull]).close().extrude(THICK).val()
    solids.append((label + "#%d" % sid, solid))

model = cq.Compound.makeCompound([s for _, s in solids])
cq.exporters.export(model, OUT)
allv = np.array([[v.X, v.Y, v.Z] for _, s in solids for v in s.Vertices()])
print("model bbox: %.2f x %.2f x %.2f, %d solids" % (
    *(allv.max(0) - allv.min(0)), len(solids)))
print("STEP written:", os.path.exists(OUT), os.path.getsize(OUT), "bytes")
