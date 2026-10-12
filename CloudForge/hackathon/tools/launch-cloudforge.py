# 通过 WMI 启动 CloudForge GUI 并让它自动加载点云（脱离 shell 的进程树清理）
# 用法: python hackathon/tools/launch-cloudforge.py <点云绝对路径> [cloudforge_exe]
# cloudforge.exe 未显式给出时按常见位置探测；alive=true 才能声称已打开。
import json, os, subprocess, sys

step_or_cloud = sys.argv[1]
# WMI 创建的进程工作目录是 system32——必须先把文件路径转成绝对路径
step_or_cloud = os.path.abspath(step_or_cloud)
exe = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("CLOUDFORGE_EXE")
if not exe:
    tool_dir = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(tool_dir))
    known = [
        os.path.join(repo, "src-tauri", "target", "release", "cloudforge.exe"),   # 本工作区是仓库副本
        r"E:\C++\Point\CloudForge\src-tauri\target\release\cloudforge.exe",       # 主仓库（精简工作区场景）
        r"E:\C++\Point\CloudForge-demo\src-tauri\target\release\cloudforge.exe",
        r"C:\CloudForge\src-tauri\target\release\cloudforge.exe",
    ]
    exe = next((c for c in known if os.path.isfile(c)), None)
if not exe or not os.path.isfile(exe):
    print(json.dumps({"ok": False, "error": "cloudforge.exe not found; build with: npm run tauri build -- --no-bundle"}))
    raise SystemExit

proc_name = os.path.splitext(os.path.basename(exe))[0]
ps = (
    '$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create '
    "-Arguments @{ CommandLine = '\"%s\" \"%s\"' }; "
    "Start-Sleep 6; "
    "$p = Get-Process %s -ErrorAction SilentlyContinue; "
    '"created=$($r.ReturnValue);alive=$([int]($null -ne $p));pid=$(if($p){$p.Id}else{0})"'
) % (exe, step_or_cloud, proc_name)

r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-c", ps],
                   capture_output=True, text=True, timeout=90,
                   encoding="gbk", errors="replace")
out = (r.stdout or "") + (r.stderr or "")
info = {}
for kv in out.replace("\n", ";").split(";"):
    if "=" in kv:
        k, _, v = kv.strip().partition("=")
        info[k.strip()] = v.strip()

alive = info.get("alive") == "1"
print(json.dumps({
    "ok": alive,
    "launched": info.get("created") == "0",
    "alive": alive,
    "pid": info.get("pid"),
    "note": "CloudForge GUI should now be visible and loading the cloud" if alive
            else "launch failed or process died; tell the user to start CloudForge GUI manually",
    "raw": out.strip()[:300],
}, ensure_ascii=False))
