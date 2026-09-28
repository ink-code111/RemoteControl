#pragma once
// ============================================================
// 协议基础定义（第一阶段）
//
// 为什么废弃 `#pragma pack(1) + 裸结构体` 的旧方案：
//   1) 结构体内存布局耦合编译器/平台对齐设置，换环境就可能解析错乱；
//   2) 旧 Packet 里的 `char body[]` 柔性数组必须配合 malloc/free 手工管理，
//      是内存泄漏与越界的重灾区；
//   3) 旧代码按主机字节序直接收发 int，隐含“双方都是小端 x86”的假设。
// 这里改为：显式小端编码 + std::vector<char> 承载 body，
// 读写边界全部受控。第二阶段此文件将整体替换为 Protobuf 生成代码，
// 上层 Packet / StreamDecoder 接口保持不变。
// ============================================================

#include <cstddef>
#include <cstdint>
#include <vector>

namespace rc::proto {

constexpr std::uint32_t kMagic      = 0x55AA77CC; // 沿用旧协议魔数，保持可识别性
constexpr std::size_t  kHeaderSize  = 12;         // magic(4) + cmd(4) + body_len(4)
constexpr std::uint32_t kMaxBodySize = 64u * 1024 * 1024; // 防御异常/恶意的 body_len

enum class Cmd : std::uint32_t {
    Screen      = 1,
    Mouse       = 2,
    Keyboard    = 4,
    TestConnect = 2026, // 心跳探测（第二阶段将扩展为正式心跳协议）
};

// ---- 显式小端读写：协议格式与内存布局彻底解耦 ----
inline void write_u32_le(std::vector<char>& out, std::uint32_t v) {
    out.push_back(static_cast<char>(v & 0xFF));
    out.push_back(static_cast<char>((v >> 8) & 0xFF));
    out.push_back(static_cast<char>((v >> 16) & 0xFF));
    out.push_back(static_cast<char>((v >> 24) & 0xFF));
}

inline std::uint32_t read_u32_le(const char* p) {
    return static_cast<std::uint32_t>(static_cast<unsigned char>(p[0]))
         | (static_cast<std::uint32_t>(static_cast<unsigned char>(p[1])) << 8)
         | (static_cast<std::uint32_t>(static_cast<unsigned char>(p[2])) << 16)
         | (static_cast<std::uint32_t>(static_cast<unsigned char>(p[3])) << 24);
}

} // namespace rc::proto
