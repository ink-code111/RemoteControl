#pragma once
// ============================================================
// 输入注入的抽象接口
//
// 为什么值得多抽这一层：
//  1) 【可测试】集成测试里绝不能让程序真的去动用户鼠标 —— 注入真实输入
//     会让自动化测试变成"随机点击你的桌面"。测试注入一个记录型实现即可；
//  2) 【可替换】将来要做审计日志、输入回放、或按权限过滤某些按键，
//     都只是换一个实现，会话层不动；
//  3) 【解耦】会话层从此不依赖任何 Win32 注入细节。
// ============================================================

#include "input_types.hpp"

namespace rc::server {

class IInputSink {
public:
    virtual ~IInputSink() = default;

    virtual void apply_mouse(const rc::input::MouseEvent& ev)    = 0;
    virtual void apply_keyboard(const rc::input::KeyboardEvent& ev) = 0;
};

} // namespace rc::server
