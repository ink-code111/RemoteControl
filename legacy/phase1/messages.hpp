#pragma once
// ============================================================
// 业务消息载荷的编解码（第一阶段；第二阶段由 Protobuf 取代）
//
// 旧代码的问题：
//   - `#pragma pack(1)` 的裸结构体直接 memcpy 收发，布局耦合编译器；
//   - body_len 不足时只拷一部分，剩余成员是未初始化的栈上垃圾值，
//     鼠标坐标/按键码可能是随机数（真实存在过的越界隐患）。
// 这里显式小端编码，且反序列化对长度做严格校验，不合法直接拒绝。
// ============================================================

#include "protocol.hpp"

#include <cstdint>
#include <optional>

namespace rc::proto {

enum class MouseAction : std::int32_t {
    Move = 1, LDown = 2, LUp = 3, RDown = 4, RUp = 5,
    MDown = 6, MUp = 7, LClick = 8, RClick = 9, MClick = 10,
    LDClick = 11, RDClick = 12, MDClick = 13,
};

struct MouseEvent {
    MouseAction  action;
    std::int32_t x;
    std::int32_t y;
};

constexpr std::size_t kMouseEventSize = 12; // action(4) + x(4) + y(4)

inline void encode_mouse(std::vector<char>& body, const MouseEvent& ev) {
    body.clear();
    body.reserve(kMouseEventSize);
    write_u32_le(body, static_cast<std::uint32_t>(ev.action));
    write_u32_le(body, static_cast<std::uint32_t>(ev.x));
    write_u32_le(body, static_cast<std::uint32_t>(ev.y));
}

inline std::optional<MouseEvent> decode_mouse(const std::vector<char>& body) {
    if (body.size() != kMouseEventSize) {
        return std::nullopt; // 严格校验，替代旧版“能拷多少拷多少”
    }
    return MouseEvent{
        static_cast<MouseAction>(read_u32_le(body.data())),
        static_cast<std::int32_t>(read_u32_le(body.data() + 4)),
        static_cast<std::int32_t>(read_u32_le(body.data() + 8)),
    };
}

struct KeyboardEvent {
    std::int32_t vk;    // Windows 虚拟键码
    std::int32_t flags; // 0=按下, 1=抬起（与旧协议保持一致）
};

inline void encode_keyboard(std::vector<char>& body, const KeyboardEvent& ev) {
    body.clear();
    body.reserve(8);
    write_u32_le(body, static_cast<std::uint32_t>(ev.vk));
    write_u32_le(body, static_cast<std::uint32_t>(ev.flags));
}

inline std::optional<KeyboardEvent> decode_keyboard(const std::vector<char>& body) {
    if (body.size() != 8) {
        return std::nullopt;
    }
    return KeyboardEvent{
        static_cast<std::int32_t>(read_u32_le(body.data())),
        static_cast<std::int32_t>(read_u32_le(body.data() + 4)),
    };
}

} // namespace rc::proto
