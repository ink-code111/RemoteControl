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

# 【2026-09-27】必须**显式给 dir**：`tempfile.mkdtemp()` 不带 dir 时落在 `%TEMP%`
# ——也就是 **C 盘**（`C:\Users\ASUS\AppData\Local\Temp`），违反本项目"临时产物一律进
# `E:\WBdata\_temp`"的目录约定（见用户级记忆）。虽然下面有 `shutil.rmtree`，但它是
# `ignore_errors=True` ⇒ 异常路径下会**静默残留**在 C 盘，而且不报错。
d = tempfile.mkdtemp(prefix="mut_inlat_", dir=r"E:\WBdata\_temp")
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
