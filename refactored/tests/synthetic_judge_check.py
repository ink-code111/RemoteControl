#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判据自身的回归：把**已知形状**的合成序列喂进 run_soak_check 的**真决策树**。

【为什么必须有这个文件】
  run_soak_check.py 的决策逻辑原本全内联在 main() 里。那种结构下有一个危险性质：
  **"长跑通过"和"为了让长跑通过而悄悄放宽了阈值"在代码上完全无法区分。**
  本文件的职责就是钉死前者 —— 给定形状 ⇒ 给定结论，且**泄漏形状必须被判不合格**。
  它测的是"这杆秤还准不准"，不是"产品有没有漏"。

【它不做什么】
  不启动产品、不读真实进程、不占 CPU（纯内存计算）。
  所以它可以在长跑进行中安全运行。

【覆盖的八类形状】
  ① 平 + 振荡           → 必须通过（否则判据是"草木皆兵"的假警报机）
  ② 全程线性泄漏        → 必须不合格
  ③ 后程才泄漏（前平后斜）→ 必须不合格（这条只有"后 50% 斜率"抓得到）
  ④ 反向对照那种陡泄漏   → 必须不合格
  ⑤ 短窗口 + 阶跃抖动    → 必须**不**判不合格（假阳性守卫的回归；
                          来自 70 s 烟测的实测假阳性，见 LATE_ABS_FRAC）
  ⑥ 分辨率守卫的**两个方向**（不可分辨 ⇒ suspect；够大 ⇒ bad）
     外加一条不变量：`analyze_metric` 的第二项必须是 R²（∈[0,1]），不是截距
  ⑦ **2 h 实测形状**：爬升→见顶→回落 → 必须通过（2026-09-27 前是假红）
  ⑧ **2 h 真泄漏 60 MB/小时** → 必须不合格（⑦ 的反向对照：放宽 ath 不许丢分辨力）

  ①~⑤、⑦~⑧ 走"合成序列 ⇒ 真决策树"；⑥ 直接喂稳健口径，专测守卫方向性。
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_soak_check import analyze_metric, classify_metric, ath_effective  # noqa: E402

PERIOD = 5.0          # 与 run_soak_check.SAMPLE_PERIOD 对齐

# 判据里"**不判不合格**"的结论集合。converged（热身期一次性增长、后程已收敛）
# 与 suspect（斜率超限但没有趋势）都只是**解释性输出**，不影响退出码。
NONFAIL = ("pass", "suspect", "converged")


def make_kept(span_s, fn, seed, srv_field="rss_mb"):
    """造一段**已过预热**的采样序列，schema 与 run_soak_check 的真实采样一致。

    `srv_field` 指定 fn 的返回值写进服务端的哪个字段 —— 这是必须的：
    判 `srv_gdi` 时若把泄漏值写进 `rss_mb`，被测的那一路（gdi）其实是常数 8，
    判据当然给"通过"。**测试造错数据会伪装成"判据失效"**，所以这里显式参数化。
    """
    rng = random.Random(seed)
    kept = []
    t = 0.0
    while t <= span_s:
        srv = {"gdi": 8, "handles": 163, "rss_mb": 26.5}
        srv[srv_field] = fn(t, rng)
        kept.append({"t": t, "srv": srv,
                     "cli": [{"gdi": 18, "handles": 120, "rss_mb": 21.0}]})
        t += PERIOD
    return kept


def verdict_of(kept, key, name, sth, ath, conv, uname, span_min=None):
    fit, rb = analyze_metric(kept, key, conv)
    return classify_metric(name, sth, ath, conv, uname, fit, rb, span_min=span_min)


# ---------------------------------------------------------------- 场景
def s_flat(t, rng):
    """平 + 振荡：无趋势。真实长跑里最常见的样子（工作集在 27~33 之间摆）。"""
    return 26.5 + rng.uniform(-3.0, 3.0)


def s_linear_leak(t, rng):
    """真泄漏：0.6 MB/分钟 ⇒ 23.5 分钟涨 +14.1 MB。"""
    return 26.5 + 0.6 * (t / 60.0) + rng.uniform(-2.0, 2.0)


def s_late_leak(t, rng):
    """**后程才泄漏**：前 15 分钟纹丝不动，之后 0.9 MB/分钟。
    这是"收敛性"判据唯一能抓到、而全轮线性拟合会低估的形状。"""
    ramp = 0.0 if t < 900 else 0.9 * (t - 900) / 60.0
    return 26.5 + ramp + rng.uniform(-1.5, 1.5)


def s_gdi_leak(t, rng):
    """反向对照那种陡泄漏：2562 个 GDI/分钟（≈ 42.7 个/秒）。"""
    return 8 + 42.7 * t


def s_gdi_wiggle(t, rng):
    """**短窗口下的假阳性形状**（照抄 70 s 烟测实测）：
    服务端 GDI 前 5 个采样点恒为 8、后 5 个恒为 6 —— 全轮只动了 **2 个**，
    但在 25 s 的后半窗上会拟合出 **−4.80 个/分钟（R²=0.50）**，
    用裸阈值 ±1.0 判就会变成"未收敛、不通过"。这是**测量分辨率**，不是趋势。
    修法见 run_soak_check.LATE_ABS_FRAC。"""
    return 8.0 if t < 25.0 else 6.0


# ---------------------------------------------------------------- 2 h 轮次
SPAN_2H = 118.5        # 2 h 减掉预热（WARMUP_MAX_S=90）⇒ 判据窗口 118.5 min
_CREST_T = 0.74        # 实测：RSS 峰值出现在全轮 74% 处


def s_crest_2h(t, rng):
    """**2 h 实测形状**（2026-09-27）：爬升 → 74% 处见顶 → 回落。

    照抄那轮的真实读数：42.5 →（1.5 h 内）80 → （最后 0.5 h）53.7，
    全轮斜率 ~16.5 MB/小时、R²≈0.43；`peak_rss` 在 68% 处停刷新、之后 38 min 未再创新高。

    ⚠️ **它必须判"通过"**。旧判据在这里产**假红**，机理与"是否泄漏"无关：
    首窗落在**爬升起点**（低）、末窗落在**回落半途**（比起点高），Δ=+11.3 > ±10
    ⇒ 被判不合格。而同一轮的全轮斜率 16.5 远低于 sth=30（斜率闸门说"不显著"）
    —— **两条闸门互相矛盾，且更严的那条（ath）是错的**（跨轮次长度用阈值）。
    """
    f = t / (SPAN_2H * 60.0)
    if f <= _CREST_T:
        base = 42.5 + 37.5 * (f / _CREST_T)
    else:
        base = 80.0 - 26.3 * ((f - _CREST_T) / (1.0 - _CREST_T))
    return base + rng.uniform(-3.0, 3.0)


def s_leak_2h(t, rng):
    """2 h 轮次下的**真泄漏**：1.0 MB/分钟 = **60 MB/小时**（sth 的 2 倍）。

    长轮次放宽 ath 之后，这条用来证明**分辨力没丢**：
    真泄漏的漂移随轮次**同比**放大，而 ath_eff 也同比放大 ⇒ 信噪比不变。
    """
    return 26.5 + 1.0 * (t / 60.0) + rng.uniform(-2.0, 2.0)


# ---------------------------------------------------------------- 可达性
def branch_reachability(span_min, sth, ath, conv):
    """分支 3/4（依赖 `|slope·conv| > sth` 且绝对变化量没先触发）是否能到达。

    分支 1（|Δ中位| > ath_eff）在最前面。要让分支 3/4 有机会执行，必须
        R ≤ ath_eff          （漂移没被最硬的闸门吃掉）
    而斜率条件要求
        R > sth/conv × span（漂移足够大）
    ⇒ 当 `sth/conv × span ≥ ath_eff` 时，分支 3/4 **不可达**：全轮有趋势就必然
      已经被分支 1 判掉了。

    ⚠️ **2026-09-27 起 ath 是缩放的**（`ath_effective`）。用缩放前的常数算这条
    曾得出"30 min 下不可达"——那在 30 min 是**无害的等价**（need 14.25 vs ath_eff 10.0），
    但在 2 h 下"不可达"意味着一件坏事：**ath 比 sth 严 5.3 倍，把斜率闸门架空**。
    """
    need = sth / conv * span_min
    ath_eff = ath_effective(ath, span_min)
    return need, need < ath_eff


# ---------------------------------------------------------------- 主流程
def main():
    ok = True
    print("[synth] 判据自身回归：合成形状 ⇒ 决策树结论")
    print()

    # ===== 场景 1~3：服务端 RSS 口径（RSS 的 conv=60，阈值 30 MB/小时 / ±10 MB）=====
    SPEC_RSS = ("srv_rss", "服务端 RSS", 30.0, 10.0, 60.0, "MB/小时")
    SPAN_30MIN = 28.5      # 30 min 轮次减掉预热（WARMUP_MAX_S=90）⇒ **实测** 28.42 min
    #   ⛔ 2026-09-27 之前这里是 23.5（按"30−1.5"推算错的）。它**同时**是 ath 的标定
    #   基准 ⇒ 两处都错时比值恰好抵消（ratio 仍 = 1.0），**回归照样全绿**。
    #   这就是"基准被算错"最难抓的地方：单看回归看不出来，必须去量真实窗长。
    cases = [
        ("① 平 + 振荡（无趋势）", s_flat,        SPAN_30MIN, None,        0),
        ("② 全程线性泄漏 0.6MB/分", s_linear_leak, SPAN_30MIN, "bad",     1),
        ("③ 后程才泄漏 0.9MB/分", s_late_leak,   SPAN_30MIN, "bad",       2),
    ]
    print(f"[synth] {'场景':<26}{'结论':<12}{'依据':<8}说明")
    for label, fn, span, want, seed in cases:
        kept = make_kept(span * 60.0, fn, 1000 + seed)
        # ⚠️ 必须把 span_min 传进去：不传 ⇒ ath 不缩放 ⇒ 测的就不是真实判据。
        kind, why = verdict_of(kept, *SPEC_RSS, span_min=span)
        got = kind or "pass"
        flag = "✅" if got == (want or "pass") else "❌"
        if got != (want or "pass"):
            ok = False
        print(f"[synth] {label:<26}{got:<12}{flag}      {(why or '全轮拟合与稳健口径都在阈值内')[:96]}")

    # ===== 场景 7~8：**2 h 轮次**（ath 缩放之后才成立的两条）=====
    #   ⑦ 是 2026-09-27 那轮长跑的**真实形状**，旧判据在这里产假红（见 s_crest_2h）。
    #   ⑧ 是"放宽 ath 之后还抓不抓得住真泄漏"的反向对照 —— 不放它，改判据就无从证伪。
    for label, fn, want, seed in (("⑦ 2h 爬升→见顶→回落（实测形状）", s_crest_2h, None, 7),
                                  ("⑧ 2h 全程真泄漏 60MB/小时", s_leak_2h, "bad", 8)):
        kept = make_kept(SPAN_2H * 60.0, fn, 3000 + seed)
        kind, why = verdict_of(kept, *SPEC_RSS, span_min=SPAN_2H)
        got = kind or "pass"
        flag = "✅" if got == (want or "pass") else "❌"
        if got != (want or "pass"):
            ok = False
        print(f"[synth] {label:<26}{got:<12}{flag}      {(why or '全轮拟合与稳健口径都在阈值内')[:96]}")

    # ===== 场景 4：反向对照（GDI 陡泄漏，90 s 短轮次）=====
    #   ⚠️ 必须把 fn 的返回值写进 **gdi** 字段；写错字段会得到一份"平的"序列，
    #      于是判据给"通过" —— 看上去像判据失效，其实是测试造错了数据。
    SPEC_GDI = ("srv_gdi", "服务端 GDI", 1.0, 20.0, 1.0, "个/分钟")
    kept = make_kept(90.0 - 22.5, s_gdi_leak, 44, srv_field="gdi")
    kind, why = verdict_of(kept, *SPEC_GDI, span_min=(90.0 - 22.5) / 60.0)
    got = kind or "pass"
    print(f"[synth] {'④ 反向对照陡泄漏(GDI,90s)':<26}{got:<12}"
          f"{'✅' if got == 'bad' else '❌'}      {(why or '')[:96]}")
    if got != "bad":
        ok = False

    # ===== 场景 5：短窗口 + 阶跃式抖动 ⇒ **不许**报不合格（假阳性守卫的回归）=====
    #   这是真实来源：70 s 烟测里服务端 GDI 全轮只动 **2 个**（8×5 → 6×5），
    #   后窗却被拟合出 −4.80 个/分钟、R²=0.50 ⇒ 判"未收敛、不通过"。**假阳性。**
    kept = make_kept(50.0, s_gdi_wiggle, 55, srv_field="gdi")
    kind, why = verdict_of(kept, *SPEC_GDI, span_min=50.0 / 60.0)
    got = kind or "pass"
    fine = got in NONFAIL
    print(f"[synth] {'⑤ 短窗口抖动(GDI,50s)':<26}{got:<12}"
          f"{'✅' if fine else '❌'}      {(why or '全轮拟合与稳健口径都在阈值内')[:96]}")
    if not fine:
        ok = False

    # ===== 场景 6：**分辨率守卫的方向性**（直接喂 rb，不经过序列）=====
    #   守卫必须"堵假阳性、放真信号"两个方向都对：
    #     · 后窗斜率超限 + 变化量不可分辨 ⇒ suspect（不判不合格）
    #     · 后窗斜率超限 + 变化量够大     ⇒ bad
    #   只测一个方向的话，谁把守卫写成"永远返回 suspect"都不会被发现。
    print()
    print("[synth] 分辨率守卫（LATE_ABS_FRAC=0.5）的方向性")
    base_rb = {"first_med": 8.0, "last_med": 6.0, "late_slope": -4.8, "late_r2": 0.99,
               "late_change": -2.0, "all_min": 6.0, "all_max": 8.0}
    for label, chg, want_ok in (("后窗只动 2 个（不可分辨）", -2.0, "suspect"),
                                ("后窗动了 25 个（可分辨）", -25.0, "bad")):
        rb = dict(base_rb, late_change=chg)
        kind, why = classify_metric("服务端 GDI", 1.0, 20.0, 1.0, "个/分钟",
                                    (0.0, 0.10, 11), rb)
        got = kind or "pass"
        same = got == want_ok
        if not same:
            ok = False
        print(f"[synth]   {label:<24}{got:<10}{'✅' if same else '❌'}"
              f"      {(why or '')[:92]}")

    # ===== 约定自检：fit 的第二项必须是 R²（∈[0,1]），不能是截距 =====
    #   元组解包错了**不报错**，只是数字变样 —— 本轮就发生过（平序列打印 R²=8.27，
    #   那其实是截距）。所以把这条不变量也钉住。
    kept = make_kept(300.0, s_flat, 7)
    fit, _rb = analyze_metric(kept, SPEC_RSS[0], SPEC_RSS[4])
    _slope, _r2, _n = fit
    conv_ok = (0.0 <= _r2 <= 1.0) and _n == len(kept)
    print(f"[synth]   {'fit 约定 (斜率,R²,n)':<24}{'—':<10}{'✅' if conv_ok else '❌'}"
          f"      R²={_r2:.3f} ∈[0,1]、n={_n}（平序列若 R²≈26 说明拿到的是截距）")
    if not conv_ok:
        ok = False

    # ===== 分支可达性（这条是**发现**，不是断言；见下方说明）=====
    print()
    print("[synth] 分支可达性（判据内部会不会有条永远走不到的分支）")
    #   need 的单位是**绝对漂移量**：RSS 是 MB，GDI/句柄是「个」。
    #   ⚠️ 2026-09-27：`ath` 现在按窗长缩放（ath_effective），所以 2 h 那一档的
    #      ath_eff 也被放大（±10 → ±41.6）。**但"不可达"仍然成立** —— need 与
    #      ath_eff 是同比放大的，遮蔽是**结构性的**。修的**不是**遮蔽本身，而是
    #      "遮蔽的代价"：缩放前 2 h 的 ath 隐含斜率只有 5.63 MB/小时（比 sth 严 5.3 倍），
    #      缩放后回到 23.4（与 30 min 的 23.45 相同）⇒ 两条闸门同口径，遮蔽变回无害的等价。
    for span_label, span_min in (("30 min 轮次", 28.5), ("2 h 轮次", 118.5), ("90 s 轮次", 1.125)):
        print(f"[synth]   {span_label}：")
        for key, name, sth, ath, conv, uname, unit in (
                SPEC_RSS + ("MB",), SPEC_GDI + ("个",)):
            need, reach = branch_reachability(span_min, sth, ath, conv)
            ath_eff = ath_effective(ath, span_min)
            tail = ("可达" if reach else
                    f"**不可达** —— 触发斜率条件需要漂移 > {need:.1f} {unit}，"
                    f"而那必然先被 ±{ath_eff:.1f} 的绝对闸门判掉")
            print(f"[synth]     {name:<10}ath_eff ±{ath_eff:>6.1f} {unit}"
                  f"  需要漂移 >{need:>7.2f} {unit} ⇒ {tail}")

    print()
    print("SYNTH_EXIT=0" if ok else "SYNTH_EXIT=1")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
