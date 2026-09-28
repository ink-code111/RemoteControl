# ============================================================
# 客户端：异步客户端 + 心跳 + 断线重连
#
# 第二阶段改动：
#   - network_client.cpp / screen_receiver.cpp 已被 async_client.cpp 取代
#     （阻塞收图线程 + 手写粘包处理 → Asio 异步读循环 + 帧头定长切流）；
#   - remote_window.cpp 不再持有网络线程，改为接收回调；
#   - 新增心跳定时器与指数退避重连，断线后窗口不再被迫关闭。
# ============================================================
add_executable(rc_client WIN32
    main.cpp
    async_client.cpp
    remote_window.cpp
)

target_link_libraries(rc_client PRIVATE
    rc_common
    ws2_32
    user32
    gdi32
    gdiplus
)

# ---- 2B 第二刀 TLS：OpenSSL 是 DLL 依赖，运行期必须能找到那两个 DLL ----
add_custom_command(TARGET rc_client POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${RC_OPENSSL_DIR}/bin/libssl-3-x64.dll"
            "${RC_OPENSSL_DIR}/bin/libcrypto-3-x64.dll"
            "$<TARGET_FILE_DIR:rc_client>"
    COMMENT "Copying OpenSSL runtime DLLs next to rc_client")
