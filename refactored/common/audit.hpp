#pragma once
// ============================================================================
//  audit.hpp —— 2B 第三刀：审计日志
//
//  【它和 logger.hpp 的关系：不是"另一个日志"，是另一种东西】
//    logger.hpp 是**给人看的调试日志**：级别会调、格式会改、内容随排障需要增删，
//    它的读者是"正在排查某个问题的我"。
//    本文件是**给人查的审计流**：字段稳定、只追加、"发生过什么"必须完整，
//    它的读者是"事后想知道谁在什么时候做了什么的我"。
//
//    把它们混在一起会立刻出两个实际问题：
//      ① 调试日志有级别过滤（`log_level=warn`）⇒ `auth_ok` / `conn_accept`
//         这类 info 事件会**整批消失**，而审计恰恰不能因为调日志级别就丢事件；
//      ② 调试日志的格式已经在被十几处判据用正则解析（本项目的 `[paint]`/`[input]`
//         行都有"不许在中间插字段"的硬规矩）⇒ 往里加内容的**代价已经很高**，
//         而审计天生需要不断加字段。
//    所以：独立 logger、独立 sink、独立文件、不受 log_level 影响。
//
//  【为什么默认关（`audit_enable=false`）】
//    与 `auth_token` / `tls_enable` / `tcp_nodelay` 同一条纪律：**默认值不能让既有
//    行为发生任何变化**。一个会凭空多出 `logs/audit.log` 的默认值，会让"19 项回归
//    跑完之后多了个文件"变成需要解释的事 —— 而本项目的回归是逐项读判据的，
//    任何未预期的副作用都会成为下一次误判的来源。
//    关闭时**连文件都不创建**（不是"创建了但不写"）—— 后者同样会让目录里多出文件。
//
//  【失效模式与本项目的对应处置】
//    "配了但没生效"是本项目的头号学费来源（§8.6 / §8.12 / §6.14 / §8.21 / §8.22.2）。
//    审计的失效形态尤其隐蔽：**没写文件与写成功了，在程序行为上完全一样**。
//    所以：
//      · `enabled()` 的结果必须在启动日志里**明确喊出来**（server/main.cpp）；
//      · 判据不读程序自我声明，直接**解析 audit.log 文件本身**（tests/run_audit_check.py）。
// ============================================================================

#include <spdlog/sinks/rotating_file_sink.h>
#include <spdlog/spdlog.h>

#include <memory>
#include <string>

namespace rc::audit {

namespace detail {

inline std::shared_ptr<spdlog::logger>& sink() {
    static std::shared_ptr<spdlog::logger> logger;
    return logger;
}

inline bool& flag() {
    static bool on = false;
    return on;
}

/// 把任意字符串压成"审计行里可以安全内联"的形态。
///
/// 【为什么必须做这一步】审计行是 `key=value` 空格分隔的：
///   客户端自称的名字（`Hello.client_name`）是**不可信输入**，它可以直接写成
///   `x role=control` —— 于是审计行里会凭空多出一个 `role=control` 字段，
///   解析方（判据 / 事后人读）会把一个 view 会话读成 control。
///   这不是"注入攻击"那么戏剧化，但它是**审计不可信**的充分条件。
///   所以只放行 `[A-Za-z0-9._-]`，其余一律替换成 `_`（含空格与引号）。
///   长度也截断：名字只用于识别，不需要保留一整段文本把日志撑爆。
inline std::string sanitize(const std::string& in, std::size_t max_len = 48) {
    std::string out;
    out.reserve(in.size());
    for (const char ch : in) {
        const bool ok = (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') ||
                        (ch >= '0' && ch <= '9') || ch == '.' || ch == '_' || ch == '-' ||
                        ch == ':' || ch == '/' || ch == '@';
        out.push_back(ok ? ch : '_');
        if (out.size() >= max_len) {
            break;
        }
    }
    return out.empty() ? std::string("_") : out;
}

} // namespace detail

/// 初始化。`enable=false` 时**不创建任何文件**，后续 `event()` 全是空操作。
/// 必须在 server.start() 之前调用一次（多 io 线程起来之后再建就会有竞态）。
inline void init(const std::string& file, bool enable) {
    if (!enable) {
        detail::sink().reset();
        detail::flag() = false;
        return;
    }
    // 轮转而不是无限增长：审计文件长到 GB 级对"事后能查到最近发生了什么"没有帮助，
    // 反而会让"打开它"这件事本身变得不可能。10 MB × 5 份对本工具量级足够。
    // ⚠️ 轮转的代价是**更早的记录会被丢掉** —— 这是刻意的取舍，不是疏漏：
    //    需要长期留存时应当由外部（例如定期把文件挪走）承担，而不是让服务端自己
    //    无限写盘。这条写在这里，免得下一个人以为"审计是全量历史"。
    auto logger = std::make_shared<spdlog::logger>(
        "rc_audit",
        std::make_shared<spdlog::sinks::rotating_file_sink_mt>(
            file, 10 * 1024 * 1024, 5));
    // 审计行自带一个稳定前缀：`<时间>  event=<名字> <字段…>`
    // 刻意**不带**级别与线程号（调试日志才有那两个）—— 审计的解析方不需要它们，
    // 而少一列就少一处会因为格式变动而打断解析的地方。
    logger->set_pattern("%Y-%m-%d %H:%M:%S.%e  %v");
    logger->set_level(spdlog::level::info);
    // 审计必须**立刻落盘**：远程控制进程被强杀是常态，而"谁在被杀之前连进来过"
    // 恰恰是那之后最需要知道的事（同 logger.hpp 里 flush_on 的实测教训）。
    logger->flush_on(spdlog::level::info);
    detail::sink() = std::move(logger);
    detail::flag() = true;
}

inline bool enabled() noexcept { return detail::flag(); }

/// 写一条审计事件。
///
/// @param name   事件名（**接口的一部分**，判据按它筛选；改动它等于改接口）
/// @param fields 已格式化的字段串，形如 `session=3 client=laptop role=view`
///
/// 为什么做成"事件名 + 一个已格式化好的字段串"而不是变参模板：
///   审计行的**字段集合是接口**（解析方按 `key=` 取值）。让它显式出现在调用点，
///   比藏在一串变参里更容易看出"这一行到底写了哪些字段"。
///   配套地，值请一律过 `sanitize()`（尤其任何来自客户端自称的字符串）。
inline void event(const char* name, const std::string& fields) {
    if (!detail::flag()) {
        return;
    }
    // 空 fields 时不留尾空格：判据用 split(' ') 解析时，尾空格会产生一个空 token。
    if (fields.empty()) {
        detail::sink()->info("event={}", name);
    } else {
        detail::sink()->info("event={} {}", name, fields);
    }
}

} // namespace rc::audit
