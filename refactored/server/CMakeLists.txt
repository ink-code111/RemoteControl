# ============================================================
# 服务端：Asio 异步多客户端
#
# 第二阶段改动：
#   - session.cpp 由"阻塞 socket + 每组线程"改写为 strand + 异步读写；
#   - 新增 asio_server.cpp（io_context 线程池 + async_accept + 优雅关闭）；
#   - 新增 session_registry.cpp（多客户端登记/限额）；
#   - input_executor / screen_capturer 改为实现接口（IInputSink / IScreenSource），
#     第三阶段换 DXGI/H.264 时只需换实现，会话层不动。
# ============================================================
add_executable(rc_server
    main.cpp
    asio_server.cpp
    session.cpp
    session_registry.cpp
    input_executor.cpp
    delta_capturer.cpp   # 抓屏管线的公共骨架（比对/裁剪/编码/基准帧/统计），两个后端共用
    screen_capturer.cpp  # GDI 后端：只实现 grab()（BitBlt + 光标）
    dxgi_capturer.cpp    # DXGI 后端：只实现 grab()（Desktop Duplication + 光标）
)

target_link_libraries(rc_server PRIVATE
    rc_common
    ws2_32    # Winsock
    user32    # SendInput / SetCursorPos / SetProcessDpiAwarenessContext
    gdi32     # BitBlt / GetDC
    gdiplus   # PNG 编码
    d3d11     # DXGI 后端：ID3D11Device / CopyResource / Map
    dxgi      # DXGI 后端：IDXGIOutputDuplication
)

# ---- 2B 第二刀 TLS：OpenSSL 是 DLL 依赖，运行期必须能找到那两个 DLL ----
# 复制到 exe 同目录，而不是要求用户改 PATH：回归脚本直接起 exe，
# 放同目录最省事、也最不容易出现"命令行能跑、测试里报 0xc0000135"这种差异。
add_custom_command(TARGET rc_server POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${RC_OPENSSL_DIR}/bin/libssl-3-x64.dll"
            "${RC_OPENSSL_DIR}/bin/libcrypto-3-x64.dll"
            "$<TARGET_FILE_DIR:rc_server>"
    COMMENT "Copying OpenSSL runtime DLLs next to rc_server")
