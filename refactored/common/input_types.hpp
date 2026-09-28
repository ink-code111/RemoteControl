#pragma once
// ============================================================================
//  input_types.hpp —— 输入事件的「领域模型」
//
//  【为什么要单独定义一套类型，而不是直接用 FlatBuffers 生成的枚举】
//    这是分层解耦的落点：业务层（输入注入）不应该认识"协议"。
//      - 协议要演进（改字段、加类型）时，只有编解码层需要动；
//      - 单元测试可以直接构造领域对象，不需要先搭一个 FlatBuffers buffer；
//      - 第四阶段换 UI 框架 / 换协议实现时，注入逻辑一行都不用改。
//    代价是一层薄薄的映射（proto_codec 里 from_wire / to_wire 各一个 switch），
//    这是值得的：它把"线上格式"和"领域概念"这两件会各自变化的事分开了。
//
//  枚举取值刻意与协议保持一致，便于人肉比对与抓包分析。
// ============================================================================

#include <cstdint>

namespace rc::input {

enum class MouseAction : std::uint8_t {
    kMove    = 0,
    kLDown   = 1,
    kLUp     = 2,
    kRDown   = 3,
    kRUp     = 4,
    kMDown   = 5,
    kMUp     = 6,
    kLDClick = 7,
    kRDClick = 8,
    kMDClick = 9,
    kWheel   = 10,
};

/// 鼠标事件（坐标已由客户端按远端分辨率映射好）
struct MouseEvent {
    MouseAction action      = MouseAction::kMove;
    std::int32_t x          = 0;
    std::int32_t y          = 0;
    /// 仅 kWheel 使用：正=上滚，负=下滚（单位与 Windows WHEEL_DELTA 一致）
    std::int32_t wheel_delta = 0;
};

/// 键盘事件（保持旧版语义：up=false 表示按下）
struct KeyboardEvent {
    std::int32_t vk = 0;
    bool         up = false;
};

const char* to_string(MouseAction a) noexcept;

} // namespace rc::input
