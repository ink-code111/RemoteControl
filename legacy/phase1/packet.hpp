#pragma once
// ============================================================
// 现代化的“协议包”对象
//
// RAII 如何解决旧版的内存问题：
//   - 旧版：malloc(sizeof(header)+body_len) + 手工 free，跨线程用
//     PostThreadMessage 传裸指针，所有权混乱，一处遗漏就是泄漏，
//     一处误用就是 Use-After-Free；
//   - 现在：body 由 std::vector<char> 自动管理生命周期，
//     Packet 是值类型，配合 std::move 在线程间零拷贝转移所有权，
//     “忘记释放”在类型系统层面不可能发生。
// ============================================================

#include "protocol.hpp"

namespace rc::proto {

struct Packet {
    Cmd               cmd;
    std::vector<char> body;

    // 序列化为线格式（帧头 + body）。一次性 reserve + insert，
    // 全程只有一次内存分配、一次 body 拷贝（第三阶段做零拷贝优化时
    // 将改为 header/body 分段发送或引入 buffer 视图）。
    std::vector<char> encode() const {
        std::vector<char> buf;
        buf.reserve(kHeaderSize + body.size());
        write_u32_le(buf, kMagic);
        write_u32_le(buf, static_cast<std::uint32_t>(cmd));
        write_u32_le(buf, static_cast<std::uint32_t>(body.size()));
        buf.insert(buf.end(), body.begin(), body.end());
        return buf;
    }
};

} // namespace rc::proto
