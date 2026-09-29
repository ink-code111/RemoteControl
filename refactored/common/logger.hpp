#pragma once
// ============================================================
// 统一日志门面：替换散落各处的 printf / OutputDebugString
//
// 旧代码的问题：
//   - printf 无时间戳、无线程号，多线程交错时无法排障；
//   - OutputDebugString 需要附加调试器才能看到，部署环境等于全盲；
//   - 高频日志（每帧打印）反而拖慢主逻辑。
// spdlog 方案：控制台彩色输出（开发）+ 滚动文件（现场排障）双通道，
// 自带级别/时间戳/线程号。业务代码只依赖 RC_LOG_* 宏，
// 后续换 sink（如远程日志）不影响任何调用点。
// ============================================================

#include <spdlog/sinks/rotating_file_sink.h>
#include <spdlog/sinks/stdout_color_sinks.h>
#include <spdlog/spdlog.h>

#include <chrono>
#include <filesystem>
#include <memory>
#include <string>
#include <vector>

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

namespace rc {

/// stdout 是否真的连着一个控制台（而不是被重定向到文件/管道）。
inline bool stdout_is_console() {
    const HANDLE h = ::GetStdHandle(STD_OUTPUT_HANDLE);
    if (h == nullptr || h == INVALID_HANDLE_VALUE) {
        return false;
    }
    return ::GetFileType(h) == FILE_TYPE_CHAR;
}

inline void init_logger(const std::string& log_file, spdlog::level::level_enum level) {
    std::vector<spdlog::sink_ptr> sinks;

    // 【为什么必须判断 stdout 到底连的是什么】
    // spdlog 的每个 sink 都由互斥锁保护。如果 stdout 被重定向到一个"没人读"的
    // 管道（父进程以 PIPE 启动子进程却不去读），管道缓冲区写满之后 write 会
    // **永久阻塞** —— 而这次阻塞是在持有 sink 互斥锁的情况下发生的。
    // 后果：一个线程卡住 → 其余所有线程的日志调用全部堵死 → 整个服务端假死，
    // 现象是"新连接能建立、却收不到任何响应，日志停在某一行不动"。
    // 这个坑真实发生过（端到端探针定位到它花了不少功夫），
    // 与其在出事后再去解读这种极具迷惑性的现象，不如在源头避免：
    // 不是控制台就只写文件。
    if (stdout_is_console()) {
        sinks.push_back(std::make_shared<spdlog::sinks::stdout_color_sink_mt>());
    }

    // 【为什么在这里建父目录】logs/ 被 .gitignore 的全局 `logs/` 规则排除，
    // 全新 clone 后并不存在；而 spdlog 的 file sink **不会创建父目录**，
    // 缺目录会让 init_logger 在启动时直接抛异常 —— 服务端 exit 1、客户端弹
    // "启动失败"，陌生人按 README 跑第一条命令就会踩中（2026-09-29 确认）。
    // 这里先把父目录建出来；真建不出来（权限/路径非法）时不吞错，
    // 让下面的 sink 抛出它自己的异常，错误信息更贴近真实原因。
    const auto log_dir = std::filesystem::path(log_file).parent_path();
    if (!log_dir.empty()) {
        std::error_code ec;
        std::filesystem::create_directories(log_dir, ec);
    }

    sinks.push_back(std::make_shared<spdlog::sinks::rotating_file_sink_mt>(
        log_file, 5 * 1024 * 1024 /*单文件 5MB*/, 3 /*保留 3 个*/));

    auto logger = std::make_shared<spdlog::logger>("rc", sinks.begin(), sinks.end());
    logger->set_level(level);
    logger->set_pattern("[%Y-%m-%d %H:%M:%S.%e][%l][t%t] %v");

    // flush_on：必须在每条 info 及以上日志后立即落盘。
    // 实测教训：不设这一项时，程序被强杀/崩溃（远程控制场景下很常见）
    // 会导致文件 sink 缓冲区里的日志全部丢失 —— 恰恰是排障最关键的那几条。
    logger->flush_on(spdlog::level::info);
    // 同时让 spdlog 后台线程周期性刷盘，兼顾性能
    spdlog::flush_every(std::chrono::seconds(3));

    spdlog::set_default_logger(std::move(logger));
}

} // namespace rc

// 直接绑定 spdlog 全局函数：运行时按级别过滤，始终参与编译，
// 避免 SPDLOG_ACTIVE_LEVEL 宏带来的“发布版日志消失”的坑。
#define RC_LOG_TRACE(...) ::spdlog::trace(__VA_ARGS__)
#define RC_LOG_DEBUG(...) ::spdlog::debug(__VA_ARGS__)
#define RC_LOG_INFO(...)  ::spdlog::info(__VA_ARGS__)
#define RC_LOG_WARN(...)  ::spdlog::warn(__VA_ARGS__)
#define RC_LOG_ERROR(...) ::spdlog::error(__VA_ARGS__)
