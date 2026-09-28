#include "network_client.hpp"

namespace rc::client {

NetworkClient::NetworkClient(net::TcpSocket socket) : socket_(std::move(socket)) {}

bool NetworkClient::send_packet(const proto::Packet& pkt) {
    const std::vector<char> wire = pkt.encode();
    std::lock_guard<std::mutex> lock(send_mutex_);
    return socket_.send_all(wire.data(), static_cast<int>(wire.size()));
}

bool NetworkClient::send_packet(proto::Cmd cmd, const std::vector<char>& body) {
    const proto::Packet pkt{cmd, body};
    return send_packet(pkt);
}

int NetworkClient::recv_some(char* data, int len) {
    return socket_.recv_some(data, len);
}

void NetworkClient::close() {
    socket_.reset();
}

bool NetworkClient::is_open() const {
    return socket_.valid();
}

} // namespace rc::client
