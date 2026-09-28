# ============================================================
# 端到端探针（rc_probe）
#
# 它是一个"用真协议说话的假客户端"：直接链接 rc_common，
# 于是和服务端/客户端共用同一份 frame.cpp / proto_codec.cpp。
# 这样协议只要改一次，探针自动跟着改 —— 不会出现
# "C++ 改了协议、Python 测试脚本还是老字节序"这种测了个寂寞的情况。
#
# 由 tests/run_local_e2e.py 负责进程编排：起 rc_server.exe -> 跑本探针 -> 收尾。
# 也可以手工指定目标地址单跑：
#     build-ninja\tests\rc_probe.exe 127.0.0.1 9999
#
# 用 option 包一层，是为了让"只想编产品不编测试"的场合可以 -DRC_BUILD_TESTS=OFF。
# ============================================================
option(RC_BUILD_TESTS "构建端到端探针 rc_probe" ON)

if(RC_BUILD_TESTS)
    add_executable(rc_probe probe_client.cpp)
    target_link_libraries(rc_probe PRIVATE rc_common)

    # ------------------------------------------------------------
    # DXGI 读回带宽 spike（第三阶段第 1 步的前置实验，见 docs/02 §6.14）
    #
    # 刻意**不链接 rc_common**：它要独立于产品代码，回答"DXGI 抓一帧到底几毫秒"。
    # 若它依赖产品代码，就得先有产品代码 —— 那就违背了"先量后改"的顺序。
    # 只用 D3D11/DXGI + User32（自建受控变化源窗口）。
    # ------------------------------------------------------------
    option(RC_BUILD_DXGI_SPIKE "构建 DXGI 读回带宽 spike" ON)
    if(RC_BUILD_DXGI_SPIKE)
        add_executable(rc_dxgi_spike experiments/dxgi_readback_spike.cpp)
        target_link_libraries(rc_dxgi_spike PRIVATE d3d11 dxgi user32)
        # WRL 的 ComPtr 与 <thread> 需要异常模型明确（MSVC 默认不带 /EHsc 时 std 头会 C4530）
        target_compile_options(rc_dxgi_spike PRIVATE /EHsc)
    endif()
endif()
