# 通过 WMI 启动 FreeCAD 打开 STEP 并验证进程存活（脱离 shell 的进程树清理）
# 用法: python hackathon/tools/preview-freecad.py <FreeCAD.exe绝对路径> <step绝对路径>
# 普通的 cmd start 启动的 GUI 会被 shell 的 Job 清理误杀；WMI Win32_Process.Create
# 由 WmiPrvSE 宿主创建，与调用方无 Job 关系，进程得以存活。
# 输出 JSON：alive=true 才能声称已打开；alive=false 时必须如实告知用户手动打开。
import json, os, subprocess, sys

exe, step = sys.argv[1], sys.argv[2]
step = os.path.abspath(step)   # WMI 创建的进程工作目录是 system32，必须绝对路径
if exe.lower().endswith("freecadcmd.exe"):
    print(json.dumps({"ok": False, "error": "pass FreeCAD.exe (GUI), not freecadcmd"}))
    raise SystemExit

ps = (
    '$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create '
    "-Arguments @{ CommandLine = '\"%s\" \"%s\"' }; "
    "Start-Sleep 5; "
    "$p = Get-Process FreeCAD -ErrorAction SilentlyContinue; "
    '"created=$($r.ReturnValue);alive=$([int]($null -ne $p));pid=$(if($p){$p.Id}else{0})"'
) % (exe, step)

r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-c", ps],
                   capture_output=True, text=True, timeout=60,
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
    "note": "FreeCAD window should now be visible to the user" if alive
            else "launch failed or process died; tell the user to open the STEP manually",
    "raw": out.strip()[:300],
}, ensure_ascii=False))
