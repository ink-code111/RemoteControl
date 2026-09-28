#include "session_registry.hpp"
#include "session.hpp"

namespace rc::server {

void SessionRegistry::add(const std::shared_ptr<Session>& session) {
    if (!session) return;
    std::lock_guard<std::mutex> lock(mutex_);
    sessions_[session->id()] = session;
}

void SessionRegistry::remove(std::uint32_t id) {
    std::lock_guard<std::mutex> lock(mutex_);
    sessions_.erase(id);
}

std::size_t SessionRegistry::size() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return sessions_.size();
}

bool SessionRegistry::at_capacity(std::size_t max_clients) const {
    std::lock_guard<std::mutex> lock(mutex_);
    return sessions_.size() >= max_clients;
}

std::vector<std::shared_ptr<Session>> SessionRegistry::snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<std::shared_ptr<Session>> out;
    out.reserve(sessions_.size());
    for (const auto& [id, s] : sessions_) {
        out.push_back(s);
    }
    return out;
}

std::vector<SessionInfo> SessionRegistry::list() const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<SessionInfo> out;
    out.reserve(sessions_.size());
    for (const auto& [id, s] : sessions_) {
        SessionInfo info;
        info.id              = id;
        info.peer            = s->peer();
        info.connected_at_ms = s->connected_at_ms();
        out.push_back(std::move(info));
    }
    return out;
}

} // namespace rc::server
