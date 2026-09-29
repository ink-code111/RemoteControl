"""一次性变异测试：证明 run_input_latency_check.py 的源码级守卫真的抓得住。

四个变异体：
  ① 判据里把 rep_last["sent_total"] 改回 rep["sent_total"]      ← 本次修的坑本体
  ② 判据里把 rep_last["missing"]    改回 rep["missing"]
  ③ 把守卫的"切法"退回到第一版（src[:find("def selftest")]）      ← 本次变异测试抓出的守卫自身 bug
  ④ 把 run_stage() 里的 split_windows(inlat_rows) 内联回去
期望：① ② ③ ④ 全部 exit=1（抓到）。
"""
import os
import shutil
import subprocess
import sys
import tempfile

PY = sys.executable
SRC = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                   "..", "..", "VsProject", "RemoteControl",
                                   "refactored", "tests", "run_input_latency_check.py"))
SRC = r"E:\VsProject\RemoteControl\refactored\tests\run_input_latency_check.py"

src = open(SRC, encoding="utf-8").read()
Q = chr(34)

CUT_NEW = (
    '    a = src.find("def selftest")\n'
    '    b = src.find("\\ndef ", a + 1) if a >= 0 else -1\n'
    '    masked = (src[:a] + src[b:]) if (a >= 0 and b > a) else src\n'
)
CUT_OLD = '    masked = src[:src.find("def selftest")]\n'

muts = []

m1 = src.replace('rep_last[' + Q + 'sent_total' + Q + ']', 'rep[' + Q + 'sent_total' + Q + ']')
if m1 != src:
    muts.append(('① 累计量 sent_total 改回代表窗口', m1))

m2 = src.replace('rep_last[' + Q + 'missing' + Q + ']', 'rep[' + Q + 'missing' + Q + ']')
if m2 != src:
    muts.append(('② 累计量 missing 改回代表窗口', m2))

if CUT_NEW in src:
    muts.append(('③ 切法退回第一版（只扫 selftest 之前）', src.replace(CUT_NEW, CUT_OLD)))
else:
    print("!! 找不到新版切法代码块，变异 ③ 无法构造")

m4 = src.replace('split_windows(inlat_rows)', 'inlat_rows')
if m4 != src:
    muts.append(('④ split_windows 规则被内联回去', m4))

print("原始文件 --selftest：", end="")
r = subprocess.run([PY, SRC, "--selftest"], capture_output=True, text=True)
print(f"exit={r.returncode}  {r.stdout.strip().splitlines()[-1][:70] if r.stdout.strip() else ''}")

# 【2026-09-27】优先用**显式给 dir**的 E 盘约定目录（作者本机"临时产物不进 C 盘"）；
# 2026-09-29 补回退：别人的机器上没有这个盘/目录，mkdir 失败就落到 %TEMP%，
# 不能因为目录约定让夹具崩掉。`shutil.rmtree(ignore_errors=True)` 在异常路径下
# 会**静默残留**临时目录且不报错 —— 这是上面那条约定本来想避免的，两害取其轻。
try:
    d = tempfile.mkdtemp(prefix="mut_inlat_", dir=r"E:\WBdata\_temp")
except OSError:
    d = tempfile.mkdtemp(prefix="mut_inlat_")
    print(f"[mut] E 盘约定目录建不出来，回退到 {d}")
ok = True
for i, (name, code) in enumerate(muts):
    p = os.path.join(d, f"m{i}.py")
    open(p, "w", encoding="utf-8").write(code)
    r = subprocess.run([PY, p, "--selftest"], capture_output=True, text=True)
    caught = (r.returncode == 1)
    ok = ok and caught
    last = (r.stdout.strip().splitlines()[-1][:78] if r.stdout.strip() else r.stderr.strip()[:78])
    print(f"[变异] {name:34s} exit={r.returncode}  {'✓抓到' if caught else '✗漏过'}  {last}")
shutil.rmtree(d, ignore_errors=True)
print("==== 变异测试结论：", "全部抓到 ✓" if ok else "存在漏网 ✗", "====")
sys.exit(0 if ok else 1)
