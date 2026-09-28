#pragma once
// ============================================================
// 会话登记表：多客户端支持的基础设施
//
// 旧版只支持单客户端（一个全局 socket + 一个接收缓冲区），
// 第二阶段起服务端要同时服务多个会话，因此必须有地方：
//   - 记录"当前有哪些会话"（用于优雅关闭与运维可见性）
//   - 限制最大并发数（防止被轻易打满）
//
// 并发说明：这是唯一跨会话共享的可变状态，用 mutex 保护。
// 绝不能在做遍历时回调 Session 的方法（可能死锁）——
// 所以对外只提供 snapshot()（拷贝出 shared_ptr 列表后在锁外使用）。
// ============================================================

#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace rc::server {

class Session;

/// 会话快照信息（无锁读取，供日志/统计使用）
struct SessionInfo {
    std::uint32_t id          = 0;
    std::string   peer;
    std::int64_t  connected_at_ms = 0;
};

class SessionRegistry {
public:
    void add(const std::shared_ptr<Session>& session);
    void remove(std::uint32_t id);

    std::size_t size() const;
    bool        at_capacity(std::size_t max_clients) const;

    /// 拷贝出当前所有会话的强引用。会话可能在你使用列表时正好关闭 —— 这是正常的，
    /// 持有 shared_ptr 保证对象不会在你手上被销毁。
    std::vector<std::shared_ptr<Session>> snapshot() const;

    /// 轻量信息列表（不持有会话引用）
    std::vector<SessionInfo> list() const;

private:
    mutable std::mutex                                     mutex_;
    std::unordered_map<std::uint32_t, std::shared_ptr<Session>> sessions_;
};

} // namespace rc::server
