"""
真机验证：菜单弹出后不选择，再次按键能否重新进入选择。
同时检查列表超时后数字键/Esc 是否被释放。

  t=0   按键 -> 开始搜索
  t=8   再按键 -> 应"复用已有结果"直接重开列表（日志有"复用上次结果"，且没有新的网络请求）
  t=35  列表超时已过（pick_timeout=20） -> 1~9 / Esc 绑定应被释放
  t=36  再按键 -> 列表重新打开
"""
import json
import os
import subprocess
import sys
import time

MPV = r"D:\tools\音视频处理\MPV1\mpv-lazy\mpv.com"
VIDEO = r"C:\Users\a6901\AppData\Local\Temp\mpvtest\SSNI-497.mp4"
PIPE = r"\\.\pipe\mpvpick2"
LOG = r"C:\Users\a6901\AppData\Local\Temp\mpvpick2.log"

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


def picker_bindings():
    r = call(["get_property", "input-bindings"])
    data = (r or {}).get("data") or []
    hits = []
    for b in data:
        cmd = str(b.get("cmd") or "")
        if "javsubs-pick" in cmd or "javsubs-pick" in str(b.get("owner") or ""):
            hits.append((b.get("key"), cmd))
    return hits


def key(k):
    call(["keypress", k])
    print(f"  [{time.time() - T0:5.1f}s] keypress {k}")


time.sleep(5)
T0 = time.time()

print("1) 首次按键")
key("Ctrl+Alt+s")
time.sleep(8)
b1 = picker_bindings()
print(f"   t=8s  选择器绑定数 = {len(b1)}  keys={[x[0] for x in b1][:12]}")

print("2) 不选择，再按一次（这是用户报的问题点）")
key("Ctrl+Alt+s")
time.sleep(3)
b2 = picker_bindings()
print(f"   t=11s 选择器绑定数 = {len(b2)}")

print("3) 等列表超时（pick_timeout=20s）后检查按键是否释放")
time.sleep(26)
b3 = picker_bindings()
print(f"   t=37s 选择器绑定数 = {len(b3)}  （0 表示 1~9/Esc 已释放）")

print("4) 超时后再按一次")
key("Ctrl+Alt+s")
time.sleep(3)
b4 = picker_bindings()
print(f"   t=40s 选择器绑定数 = {len(b4)}  （>0 表示列表能重新打开）")

call(["quit"])
time.sleep(1)
try:
    proc.terminate()
except Exception:
    pass
logf.close()

print("\n=== 判定 ===")
res = {
    "首次搜索弹出列表": len(b1) > 0,
    "再次按键重开列表": len(b2) > 0,
    "超时后按键已释放": len(b3) == 0,
    "超时后可重新打开": len(b4) > 0,
}
for k, v in res.items():
    print(("PASS " if v else "FAIL ") + k)
print("全部通过" if all(res.values()) else "有失败项")
