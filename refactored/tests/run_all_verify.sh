#!/bin/bash
# 全套回归：DPI 预检 + 构建 + 光标合成 + e2e + GUI + vcxproj + 写队列竞态
#          + 差异帧正确性（gdi/dxgi 两轮）+ DXGI 超时复用判据 + 延迟维度
#          + 整帧优先通道 + 输入→显示延迟 + 输入优先抓屏 + 2B 认证 + 2B TLS
#          + 客户端按脏区重绘 + 2B 权限模型与审计日志 + 收尾清理
#
# 打出的 *_EXIT= 行（每轮共 20 行）：
#   DPI_PREFLIGHT / BUILD / CURSOR / E2E / GUI / VCXPROJ / RACE
#   DELTA_GDI / DELTA_DXGI / DELTA / DXGI_TIMEOUT / LATENCY / KEYFRAME
#   INPUT_LATENCY / INPUT_PRIORITY / AUTH / TLS / PARTIAL_REPAINT / ACL / DPI_POSTFLIGHT
#   全 0 即绿。
#
# DELTA 是 DELTA_GDI 与 DELTA_DXGI 的合成（都 0 -> 0；任一 1 -> 1；否则 2），
# 见 [7/17] 处的说明。要定位是哪一轮出问题，看那两个分项。
#
# 倒数第二项之前的四项（KEYFRAME / INPUT_LATENCY / INPUT_PRIORITY / PARTIAL_REPAINT）
#   都会**驱动物理光标与鼠标输入**（PARTIAL_REPAINT 靠自动输入源让画面变化）：
#   同机自测时服务端 SetCursorPos 移动的就是本机光标。跑回归期间别用鼠标。
#   （AUTH 与 TLS 不驱动光标，但它们会各弹 4 次客户端窗口。）
#   新增的 ACL 项**不驱动光标**（它那几轮要么不发输入，要么全被服务端按只读拒掉），
#   但要弹 6 次客户端窗口、耗时约 70 s，见 [16/17] 处的说明。
#
# 0) 预检 DPI 兼容层：Windows 会在某个 exe **跑过一次 DXGI 之后**按路径给它加
#    HIGHDPIAWARE，此后该 exe 每次启动都是 DPI-aware —— 配置 dpi_aware=false 被架空，
#    帧尺寸随"运行顺序"变化。这种污染在日志里看不出来，所以每轮开跑前先查/清一次。
#    （见 tests/check_dpi_override.py 的说明）
export PATH="/c/Users/ASUS/.workbuddy/binaries/PortableGit/versions/1.2.0/bin:/c/Users/ASUS/.workbuddy/binaries/PortableGit/versions/1.2.0/usr/bin:/c/Windows/System32:/c/Windows:/c/Windows/System32/WindowsPowerShell/v1.0:$PATH"

CMAKE="/e/vs/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
NINJA="/e/vs/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe"
PY="/c/Users/ASUS/.workbuddy/binaries/python/versions/3.13.12/python.exe"

cd /e/VsProject/RemoteControl/refactored || exit 99

echo "===== [0/17] DPI COMPAT-LAYER PREFLIGHT ====="
"$PY" tests/check_dpi_override.py --clean
echo "DPI_PREFLIGHT_EXIT=$?"

echo
echo "===== [1/17] BUILD ====="
"$CMAKE" --build build-ninja -j 8
echo "BUILD_EXIT=$?"

echo
echo "===== [2/17] CURSOR OVERLAY ====="
"$PY" tests/run_cursor_overlay_check.py
echo "CURSOR_EXIT=$?"

echo
echo "===== [3/17] LOCAL E2E ====="
"$PY" tests/run_local_e2e.py
echo "E2E_EXIT=$?"

echo
echo "===== [4/17] CLIENT GUI ====="
"$PY" tests/run_client_gui_check.py
echo "GUI_EXIT=$?"

echo
echo "===== [5/17] VCXPROJ ====="
"$PY" tools/check_vcxproj.py
echo "VCXPROJ_EXIT=$?"

echo
echo "===== [6/17] WRITE-QUEUE RACE (close mode) ====="
"$PY" tests/run_write_race_check.py --mode close --rounds 20
echo "RACE_EXIT=$?"

echo
echo "===== [7/17] DELTA FRAME CORRECTNESS（两个抓屏后端都跑）====="
# 需要桌面能被本脚本自己造的窗口改变（脚本会弹一个 420x300 的纯色置顶窗口来回平移）。
# 退出码 2 = 无判别力（不是链路问题），照样报出来，便于区分"没测到"和"测出错"。
#
# 【为什么要跑两遍】判据的作用域是夹具行程带，但"帧里能看到什么"两个后端不同：
#   gdi  帧 = 物理屏左上角 1707x960 的 1:1 裁剪（单帧被位图覆盖 = 已通过回归的老路径）；
#   dxgi 帧 = 整块物理屏 2560x1440，且是**产品默认值（auto）实际走的那条路**。
# 只跑其中一个，就等于把另一半画面路径的回归让给运气。
# 注意 run_delta_check.py 自己会清 DPI 兼容层：不清的话，本回归里前面任何一次
# dxgi 运行都会让 rc_server.exe 之后每次启动都是 aware，gdi 那轮就不再是"裁剪帧"了。
"$PY" tests/run_delta_check.py --backend gdi
DELTA_GDI_EXIT=$?
echo "DELTA_GDI_EXIT=$DELTA_GDI_EXIT"
"$PY" tests/run_delta_check.py --backend dxgi
DELTA_DXGI_EXIT=$?
echo "DELTA_DXGI_EXIT=$DELTA_DXGI_EXIT"
# 合成一个总的 DELTA_EXIT（保持"每项一行 *_EXIT=，全 0 即绿"的口径）：
#   两者都 0 -> 0；任一为 1（链路真坏）-> 1；否则 2（没测到）。
if [ "$DELTA_GDI_EXIT" -eq 0 ] && [ "$DELTA_DXGI_EXIT" -eq 0 ]; then
  DELTA_EXIT=0
elif [ "$DELTA_GDI_EXIT" -eq 1 ] || [ "$DELTA_DXGI_EXIT" -eq 1 ]; then
  DELTA_EXIT=1
else
  DELTA_EXIT=2
fi
echo "DELTA_EXIT=$DELTA_EXIT"

echo
echo "===== [8/17] DXGI TIMEOUT MUST STILL PRODUCE FRAMES ====="
# 判据：AcquireNextFrame 超时（桌面没变）时必须复用上一份像素继续出图。
# 若把超时当失败，session 会 ok=false -> 什么都不发 -> 客户端不再请求下一帧
# -> 画面**永久冻结**（反向对照实测：只失败一次就够）。
# 退出码 1 = 判据不通过；2 = 本轮没出现超时（没测到），不混进 0/1。
"$PY" tests/run_dxgi_timeout_check.py --seconds 15
DXGI_TIMEOUT_EXIT=$?
# 退出码 2 = 本轮桌面一直有活动、一次超时都没出现（**没测到**，不是失败）。
# 这是本判据的固有性质：它要的前提就是"桌面不变"，而桌面上总有东西在动，
# 所以它天然会偶尔报 2。允许重试一次。
# **只重试 2，绝不重试 1** —— 1 是真的坏了，靠重跑"变绿"等于把失败藏起来。
if [ "$DXGI_TIMEOUT_EXIT" = "2" ]; then
  echo "[verify] 本轮桌面一直有活动（超时复用 0），判据没测到 —— 重试一次…"
  "$PY" tests/run_dxgi_timeout_check.py --seconds 15
  DXGI_TIMEOUT_EXIT=$?
fi
echo "DXGI_TIMEOUT_EXIT=$DXGI_TIMEOUT_EXIT"

echo
echo "===== [9/17] LATENCY（帧间隔抖动分布 + resync 冻结时长）====="
# 三段式：正向 / 造抖动 / 造队列溢出。后两段就是这条判据的**反向对照** ——
# 故意把链路弄坏，判据必须看得见（P95 被拉出双峰、冻结被量到且有上界）。
# 为什么非得造：回环 TCP 不丢包、静止桌面下到达间隔很整齐，这两种现象在本机
# 几乎不会自然发生 —— 不造就等于这条判据从没被执行过。
# 退出码 0/1/2 = 通过/不通过/没测到。
"$PY" tests/run_latency_check.py --seconds 12
echo "LATENCY_EXIT=$?"

echo
echo "===== [10/17] KEYFRAME PRIORITY（整帧会不会被队列溢出吞掉）====="
# 判据：**整帧在没被应用之前，绝不能被丢掉**（它是 resync 唯一的解药）。
# 两轮对照，唯一的差别就是那个开关：
#   on （产品默认）整帧走独立通道，溢出清不到它 -> 整帧被丢弃必须 == 0
#   off（旧路径）  整帧与增量帧同队列            -> 必须能看到整帧被吞（否则本轮没判别力）
# off 那一轮不是"可选加固"：**没造出过坏现象，就不知道判据有没有判别力**。
# 它同时也把 §8.17 那个"冻结没有上界"从推断变成了实测（off 轮画面会彻底停死）。
# 退出码 0/1/2 = 通过/不通过/没测到。
"$PY" tests/run_keyframe_priority_check.py --seconds 15
echo "KEYFRAME_EXIT=$?"

echo
echo "===== [11/17] INPUT LATENCY（输入→显示；两轮反向对照）====="
# 判据：一次输入从"真正进了 socket"到"被画到窗口上"隔了多久 —— 最贴近手感的一个数，
# 也是前面所有 fps / 带宽 / 帧周期都不回答的那个问题。
# 两轮的唯一差别是服务端抓屏帧率（30 vs 10）：抓屏是客户端请求驱动的，输入落在两次
# 抓屏之间就得等下一拍，所以**降帧率必须把端到端延迟抬起来**。这是对真实机制做对照，
# 而不是往链路里 sleep 出来的假延迟（后者只能证明判据会做加法）。
# 服务端侧还会独立给出"应用→抓屏 空档"，它必须与客户端测到的变化同向 —— 两条独立
# 来源一致才敢下结论。
# 前置不变式（不成立报 2，不报 0）：坐标读回**零不符**（抓屏与输入在同一坐标空间）、
# 光标**可见**（否则"移动光标"在画面里毫无痕迹，延迟测的是"帧到了"而不是"输入被看见了"）。
# ⚠️ 2026-09-24：本项**显式 pin 住 input_priority_capture=off**（夹具里 PIN_INPUT_PRIORITY）。
#    上面那条反向对照的前提（延迟 ∝ 抓屏节拍）正是第 12 项那个开关要打破的东西 ——
#    不 pin 的话它会报 1，而那看起来像功能回归。详见 tests/run_input_latency_check.py 文件头。
# 退出码 0/1/2 = 通过/不通过/没测到。
"$PY" tests/run_input_latency_check.py --seconds 12
echo "INPUT_LATENCY_EXIT=$?"

echo
echo "===== [12/17] INPUT PRIORITY CAPTURE（输入优先抓屏；A→B→A2 三轮）====="
# 判据：把 2b 量出来的**头号项**（"输入落在两次抓屏之间白等"，30 fps 下 19.7 ms、
# 占端到端 55.1 ms 的 36%）拿掉 —— 输入一被应用就立刻抓一帧，不等客户端请求与限流时刻。
# 【为什么是三轮不是两轮】这个量对机器负载很敏感：同一份代码同一配置实测出现过
# 55.1 与 71.4 ms 两轮（约 30% 漂移）。两轮 A/B 在那种漂移下没有归因能力 ——
# 所以跑 A(off) → B(on) → C(off 回照)，要求 A 与 C 自洽，且 **B 必须优于两轮 off 的均值**。
# 回照不自洽就报 2（没测到），绝不报"通过"：那种差值无法归因。
# ⚠️ 另有 I8：两轮 off 的**内容可比**（"变化帧净工作"差 ≤ 8 ms/帧），**判定在 I6 之前**。
#    回照只保证"同配置"，不保证"同内容"（用光标当可见响应时脏区 ∝ 跳距、与抓屏相位拍频）。
#    回照自洽的阈值 2026-09-25 从"相对 35%"改成"绝对 11.7 ms"。
#    ⚠️ 2026-09-26：I6 的 11.7 与 I8 的 8.0 **已从 P1 门槛上解耦**（此前写作"19.7 − 8"）。
#    它们是**两个不同空间**的量，不能随 P1 的门槛一起动（否则 I8 会被拖进它自己
#    标定表的健康群里，漏掉"两轮内容差 10.3 ms"那种真坑）。P1 门槛已按实测重标定为 3.0。
#    推导 / 标定表 / 为什么不是 8 也不是 12% 见 docs/02 §6.22(5)；假红根治见 §6.32。
# 还要验"开销守恒"：长期平均帧率必须不变（配额是被预支、不是被取消），
# 以及开关**能自证生效**（on 轮必须有输入触发抓屏计数，off 轮必须恰好为 0）。
# ⚠️ 另有 I7：服务端自报的**预支深度必须 = 定版值 1 拍**。它 2026-09-24 从编译期常量
#    升成了配置项（`input_priority_max_borrow`），于是"这一轮按几拍在跑"变成能静默改变的事；
#    本项全部数字都按 1 拍测的，不匹配就报 2（没测到），不报 0/1。
#    敏感性扫描（0/1/2/3）见 tests/experiments/borrow_sweep.py，**不入回归**。
# 退出码 0/1/2 = 通过/不通过/没测到。
"$PY" tests/run_input_priority_check.py --seconds 12
echo "INPUT_PRIORITY_EXIT=$?"

echo
echo "===== [13/17] AUTH（2B：配了 auth_token 时，凭据不对必须被拒）====="
# 判据：四轮（ok / bad / missing / off），每轮一套独立的 server + client，独立端口。
#
# 为什么要单独立项：认证的失效模式不是"报错"，而是"**静默放行**" —— 校验没跑、
# 比较写反、配置没读到，这三种情况表现**完全一样**：带错凭据的客户端照样连上，
# 而且从外面看一切正常。所以本项的重点是"该拒的时候必须拒，理由还要指对方向"
#（"客户端没配 token" 与 "token 配错了" 是两条不同的排查路径）。
#
# 三条**独立来源**同时判：客户端日志、服务端日志、**窗口标题**
#（理由必须能传到界面上；有一条 PASS 的断言就是它）。
# 反向轮（bad / missing）另有两条硬断言：「tcp connected 恰好 1 次」+
#「not reconnecting (unrecoverable)」—— 凭据错是**不可重试**的；少了它们，
# "客户端疯狂重连刷屏"也能算通过，而那正是用户看到的最没意义的一种表现。
#
# ⚠️ 本项会弹 **4 次客户端窗口**（每次约 4 秒）。
# 反向对照（**手动跑，不入回归**，因为它期望的是"失败"）：
#     python tests/run_auth_check.py --reverse-control   # 清空服务端 token，必须 FAIL
# 退出码 0/1/2 = 通过/不通过/没测到。
"$PY" tests/run_auth_check.py
echo "AUTH_EXIT=$?"

echo
echo "===== [14/17] TLS（2B 第二刀：加密必须真的生效；指纹不匹配必须拒绝）====="
# 判据：四轮（on / badpin / mismatch / off），每轮一套独立的 server + client、
# 独立端口、独立自签证书。
#
# 为什么要单独立项 —— TLS 的失效模式是"**看起来成功了**"：
#   ① 配置写了 tls_enable 但某一侧没生效（键名写错/改错文件）⇒ 链路仍是明文，
#      而日志上一切正常；
#   ② 客户端 pin 的校验回调**从没被调用过**（或写成了恒真）⇒ 加密还在、认证没了，
#      中间人可冒充服务端，外面完全看不出来；
#   ③ 明文服务端遇上 TLS 客户端时"将就一下"继续跑（静默降级）。
#   ⇒ 所以本项要的不只是"能连上"，而是**密码学证据**（客户端日志里 SSL_get_version()
#     报出的协商版本 —— 它不可能在"没真正握手"的情况下产生）＋ 另一进程的证据
#     （服务端 `session N started, ..., TLS`）＋ 界面证据（标题显示"已连接"）。
#
# ⭐ badpin 与 mismatch 的区别是本项最有信息量的一条：同样是"握手失败"，
#   前者是**配置分歧**（重连一万次结果一样）⇒ 断言 tcp connected == 1 且出现
#   `not reconnecting (unrecoverable)`；后者是**链路暂时故障**（对端可能是明文服务端，
#   也可能是正在重启）⇒ 断言 tcp connected >= 2（在重试）。少了这条区分，
#   "客户端疯狂重连刷屏"与"客户端永久躺死不重连"就都能算通过。
#
# ⚠️ 本项会弹 **4 次客户端窗口**（每次约 5 秒）。不驱动物理光标。
# ⚠️ 2026-09-25 首次运行就靠本项的 **on 轮（正向对照）** 抓出一个真 bug：
#    pin 校验回调把"已经算好的摘要"又哈希了一遍 ⇒ 指纹永远匹配不上，
#    而只跑 badpin 那种"期望失败"的轮次时它看起来完全正常。
#    教训已写进 tls.cpp：**正向对照轮不是可选项**。
#
# 反向对照（**手动跑，不入回归**，因为它期望的是"失败"）：
#     python tests/run_tls_check.py --reverse-control   # 服务端 tls_enable 一律关，必须 FAIL
# 退出码 0/1/2 = 通过/不通过/没测到。
"$PY" tests/run_tls_check.py
echo "TLS_EXIT=$?"

echo
echo "===== [15/17] 客户端按脏区重绘（部分重绘的画面必须与整幅重绘逐像素一致）====="
# 判据：两轮 A（partial_repaint=false）→ B（=true），每轮一套独立的 server + client、独立端口。
#
# 被测的是什么：客户端 image_ 里**永远是完整**画面，所以这一项动的不是合成，而是
# WM_PAINT 那一次 StretchBlt 的**落笔范围** —— 原来每帧整窗失效（12 ms 全额付出），
# 现在只失效"这一帧改了的那一块"映射出来的客户区矩形；BeginPaint 返回的 HDC 自带
# 更新区裁剪，于是同一个 StretchBlt 调用自动只写那一块（GDI 会按裁剪跳带）。
#
# 为什么必须判"画面像素"而不只是判耗时：耗时下降也可能来自别的原因（比如换了缩放
# 模式）。本项唯一的代价风险是**漏画** —— 失效矩形没覆盖到某个该重绘的目标像素，
# 那里就留下**永久残影**；而残影只在图像边缘、只有几个像素，人眼几乎不会发现。
#
# 怎么做到"时间间隔消掉"（不靠外部截屏，§8.12 实测换进程抓帧会造出假差异）：
# 客户端在**同一个 image_、同一个临界区**里落两份图 ——
#   _paint_N = 窗口客户区**实际**内容（裁剪之后真正落笔的结果）
#   _ref_N   = 用**同一份 image_**做的整幅缩放（不带本次裁剪）
# 两者只差"裁剪"这一件事 ⇒ 逐像素严格相等 ⇔ 没漏画。实测这条路径是**逐位**的。
#
# 三轮结果的分工（缺一不可）：
#   A（关）：①「实画面积」必须 ≈100%（证明开关真的退回旧行为）；
#           ② 两图必须严格相等 —— 这是"落盘 + 比对"这套机器本身的前向对照，
#              否则 B 轮的"相等"可能是机器坏了导致的假绿。
#   B（开）：①「实画面积」必须明显 < 100%；② 两图严格相等（**主判据**）。
#   ⚠️ B 轮还必须**至少有一对落在部分重绘上**（裁剪 < 99%），否则桌面就是静止的、
#      那几对全是整窗重绘，"相等"是废话 ⇒ 报 2（没测到），绝不报通过。
#
# ⚠️ 本项**不能最小化客户端窗口**（要读窗口客户区的实际像素）——
#    与 run_delta_check 正好相反（那个读内存里的累积画面，最小化更稳）。
# ⚠️ 本项驱动**物理光标**（自动输入源是它唯一的变化源），并会弹 2 次客户端窗口。
#
# 反向对照（**手动跑，不入回归**，因为它期望的是"失败"）：
#     python tests/run_partial_repaint_check.py --reverse-control   # halo=-2，必须看见残影
# 退出码 0/1/2 = 通过/不通过/没测到。
"$PY" tests/run_partial_repaint_check.py
echo "PARTIAL_REPAINT_EXIT=$?"

echo
echo "===== [16/17] ACL（2B 第三刀：只读会话的输入必须在服务端被拒 + 审计日志留痕）====="
# 判据：六轮（control / view_dbg / view_self / reject / legacy / acl_noaudit），
# 每轮一套独立的 server + client、独立端口、独立工作目录。
#
# 为什么要单独立项 —— 这一刀有两个**静默**失效模式，都不会报错：
#   ① 权限模型"拦不住"：role 没解析到、判断写反、或者拦截点被放到了**客户端**。
#      第三种最阴：对老实客户端甚至"功能上看起来是对的"（它本来就自律不发）。
#      ⇒ 判据必须把"**客户端自律**"（省带宽）与"**服务端拦截**"（安全边界）
#        拆成两条独立可证的断言。做法是同一个自动输入源、同一个凭据，只差客户端
#        `debug_ignore_role` 一个开关，两轮读**同一行的两个字段**：
#            view_dbg   本段自动源发 78 / 已发总数 78   → 服务端 denied_mouse>0
#            view_self  本段自动源发 107 / 已发总数 0   → 服务端 0 条（客户端本地丢弃 116）
#   ② 审计"没记 / 记了但读不到"：audit_enable 没读到、sink 建在了被 log_level
#      过滤的 logger 上、文件没落盘 —— 表现全是"一切正常，只是事后查不到"。
#      ⇒ 判据不是只看"文件存在"，而是按**事件名 + 字段**逐条断言
#        （auth_ok / auth_fail / input_denied / session_end），外加一条格式不变式：
#        audit 里**每一行**都必须匹配"时间戳 + event= + key=value"严格格式
#        —— 客户端自称名是不可信输入，没有 sanitize 就能伪造字段。
#
# 反向对照（**手动跑，不入回归**，因为它们期望的是"失败"）：
#     python tests/run_audit_check.py --reverse-control   # role 全改成 control，必须 FAIL
#     python tests/run_audit_check.py --reverse-audit     # audit_enable 全关，必须 FAIL
#   实测（2026-09-26）两个反向对照都红了（分别 19 / 8 条断言），且方向正确：
#   --reverse-audit 下**权限拦截的断言仍全绿**（权限与审计确实是两个独立开关）。
#
# 退出码 0/1/2 = 通过/不通过/没测到。⚠️ 报 2 的常见原因有五种，其中两种是本项专属：
#   「源码比 exe 新」（跑在陈旧二进制上）与「自动输入源没发出去」
#   （那样"服务端 0 条"分不清是拦住了还是没人在试）。**看到 2 先读理由，别读成通过。**
"$PY" tests/run_audit_check.py
echo "ACL_EXIT=$?"

echo
echo "===== 收尾：清掉本轮 dxgi 运行自动产生的兼容层 ====="
"$PY" tests/check_dpi_override.py --clean >/dev/null 2>&1
echo "DPI_POSTFLIGHT_EXIT=$?"
