# ============================================================
# 公共库：帧编解码、协议编解码、日志、配置、输入领域模型
#
# 第二阶段从 INTERFACE（纯头文件）改为 STATIC：
# 因为新增了 frame.cpp / proto_codec.cpp 两个实现文件。
# 服务端与客户端共用同一份协议实现 —— 旧工程"两份复制粘贴、略有差异的协议代码"
# 正是版本不一致、抓包对不上之类问题的温床。
# ============================================================
add_library(rc_common STATIC
    frame.cpp
    proto_codec.cpp
    tls.cpp      # 2B 第二刀 TLS：OpenSSL 上下文与证书工具
)

# 第三方依赖路径（由根 CMakeLists 统一声明 third_party 位置）
set(RC_ASIO_INC        "${RC_THIRD_PARTY_DIR}/asio/include")
set(RC_FLATBUFFERS_INC "${RC_THIRD_PARTY_DIR}/flatbuffers/include")
set(RC_PROTO_GEN_INC   "${CMAKE_CURRENT_SOURCE_DIR}/../proto/generated")
# 2B 第二刀 TLS：OpenSSL 预编译（RC_OPENSSL_DIR 由根 CMakeLists 校验并声明）
set(RC_OPENSSL_INC     "${RC_OPENSSL_DIR}/include")
set(RC_OPENSSL_LIB_DIR "${RC_OPENSSL_DIR}/lib")

foreach(_inc IN ITEMS "${RC_ASIO_INC}/asio.hpp"
                     "${RC_FLATBUFFERS_INC}/flatbuffers/flatbuffers.h"
                     "${RC_PROTO_GEN_INC}/rc_protocol_generated.h"
                     "${RC_OPENSSL_INC}/openssl/ssl.h")
    if(NOT EXISTS "${_inc}")
        message(FATAL_ERROR
            "缺少依赖头文件：${_inc}\n"
            "  third_party 依赖：python tools/fetch_phase2_deps.py\n"
            "  OpenSSL 预编译  ：python tools/fetch_openssl.py\n"
            "  协议生成代码  ：python tools/gen_proto.py")
    endif()
endforeach()

target_include_directories(rc_common PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_include_directories(rc_common PUBLIC ${RC_PROTO_GEN_INC})

# SYSTEM：把第三方头当"外部头"对待（MSVC 下走 /external:I）。
# 好处是我们自己的代码保持 /W4 的严格检查，而 asio / flatbuffers 内部的
# 告警不会淹没日志 —— 既不漏问题，也不被噪音逼得降低警告等级。
target_include_directories(rc_common SYSTEM PUBLIC
    ${RC_ASIO_INC}
    ${RC_FLATBUFFERS_INC}
    ${RC_OPENSSL_INC}
)

# UNICODE / _UNICODE：VS 的"Windows 桌面应用程序"模板会自动定义这两个宏，
# CMake 不会。本项目全程使用显式宽字符 API（...W 系列 + L"..." 字面量），
# 缺少该宏时 IDC_ARROW / IDI_APPLICATION 会被解析成 ANSI 版（LPSTR），
# 传给 LoadCursorW 就会编译报错。
# ASIO_STANDALONE：告诉 Asio 不要去找 Boost（本工程用 standalone 版本）。
target_compile_definitions(rc_common PUBLIC
    WIN32_LEAN_AND_MEAN
    NOMINMAX
    UNICODE
    _UNICODE
    ASIO_STANDALONE
)

# 2B 第二刀 TLS：OpenSSL 导入库所在目录（PUBLIC 传播到 server/client 的最终链接行）
target_link_directories(rc_common PUBLIC "${RC_OPENSSL_LIB_DIR}")

target_link_libraries(rc_common PUBLIC
    spdlog::spdlog
    nlohmann_json::nlohmann_json
    ws2_32      # Asio 在 Windows 上的底层 Winsock
    mswsock     # AcceptEx 等扩展（Asio 部分实现会用到）
    # ---- 2B 第二刀 TLS ----
    # 刻意用**裸库名**而不是 imported target（OpenSSL::SSL）：
    # tools/check_vcxproj.py 的 2c 用 "token.lower() + .lib" 反推 vcxproj 该写什么库名，
    # imported target 会推出 "openssl::ssl.lib" 这种不存在的名字。
    # 裸名 libssl -> libssl.lib，正好与 third_party/openssl/lib 下的文件同名。
    # ⚠️ 只要这两个：DLL 自身的 crypt32/bcrypt/ws2_32 等依赖由 Windows 加载器解决，
    #    链接期不需要（本项目 §8.21 那条"只链真正需要的库"的规矩）。
    libssl
    libcrypto
)
