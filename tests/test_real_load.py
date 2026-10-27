"""
真机验证（走用户真实路径：数字键选择器）：
  1) 按键 -> 等列表 -> 按 1 -> 应下载并 sub-add 字幕
  2) 再按键 -> 按 Esc -> 列表关闭，Esc 应归还给 mpv 本身
"""
import json
import os
import subprocess
import sys
import time

MPV = r"D:\tools\音视频处理\MPV1\mpv-lazy\mpv.com"
WORK = r"C:\Users\a6901\AppData\Local\Temp\mpvpick3"
VIDEO = os.path.join(WORK, "SSNI-497.mp4")
PIPE = r"\\.\pipe\mpvpick3"
LOG = r"C:\Users\a6901\AppData\Local\Temp\mpvpick3.log"

os.makedirs(WORK, exist_ok=True)
for f in os.listdir(WORK):
    if f.lower().endswith((".srt", ".ass", ".ssa")):
        os.remove(os.path.join(WORK, f))
if not os.path.exists(VIDEO):
    import shutil
    shutil.copy(r"C:\Users\a6901\AppData\Local\Temp\mpvtest\SSNI-497.mp4", VIDEO)

logf = open(LOG, "wb")
proc = subprocess.Popen([
    MPV, "--vo=null", "--ao=null", "--keep-open=yes",
    "--msg-level=all=v", "--script-opts=javsubs-debug=yes",
    "--input-ipc-server=" + PIPE, VIDEO,
], stdout=logf, stderr=subprocess.STDOUT)

f = None
for _ in range(120):
    try:
        f = open(PIPE, "r+b", buffering=0)
        break
    except OSError:
        time.sleep(0.25)
if not f:
    print("IPC 连接失败")
    proc.kill()
    sys.exit(1)

_rid = [0]


def call(cmd):
    _rid[0] += 1
    rid = _rid[0]
    f.write((json.dumps({"command": cmd, "request_id": rid}) + "\n").encode())
    f.flush()
    while True:
        line = f.readline()
        if not line:
            return None
        m = json.loads(line.decode("utf-8", "replace"))
        if m.get("request_id") == rid:
            return m


def esc_owner():
    r = call(["get_property", "input-bindings"]) or {}
    for b in r.get("data") or []:
        if str(b.get("key")).upper() == "ESC":
            return b.get("owner") or b.get("cmd")
    return None


time.sleep(5)
T0 = time.time()

print("1) 搜索并选第 1 条")
call(["keypress", "Ctrl+Alt+s"])
time.sleep(8)
call(["keypress", "1"])
# 等下载完成
files = []
for _ in range(40):
    time.sleep(1)
    files = [x for x in os.listdir(WORK) if x.lower().endswith((".srt", ".ass", ".ssa"))]
    if files:
        time.sleep(2)
        break
print(f"   落盘文件: {files}")

print("2) 再搜索一次，然后按 Esc")
call(["keypress", "Ctrl+Alt+s"])
time.sleep(4)
owner_open = esc_owner()
call(["keypress", "Esc"])
time.sleep(2)
owner_closed = esc_owner()
print(f"   列表打开时 Esc 归属: {owner_open}")
print(f"   按 Esc 后 Esc 归属  : {owner_closed}")

call(["quit"])
time.sleep(1)
try:
    proc.terminate()
except Exception:
    pass
logf.close()

print("\n=== 判定 ===")
res = {
    "按数字键完成了下载": bool(files),
    "字幕已挂载": os.path.exists(LOG) and "字幕已加载" in open(LOG, encoding="utf-8", errors="replace").read(),
}
print(("PASS " if res["按数字键完成了下载"] else "FAIL ") + "按数字键完成了下载")
print(("PASS " if res["字幕已挂载"] else "FAIL ") + "字幕已挂载")
print("全部通过" if all(res.values()) else "有失败项")
