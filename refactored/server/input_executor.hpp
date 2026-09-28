#pragma once
// ============================================================
// 输入注入执行器：把「领域事件」转换为系统输入
//
// 相对第一阶段的改动：
//  1) 不再依赖协议类型（改用 rc::input 领域模型），编制解码与注入彻底解耦；
//  2) 从「每会话一个实例的无状态静态函数」改为「服务端共享单实例 + 内部互斥」。
//     原因：第二阶段起支持多客户端，若两个客户端同时注入，
//     各自的 SendInput 序列会交错，出现"按下属于 A、抬起属于 B"的状态错乱。
//  3) 双击从 4 次独立 SendInput 改为 1 次数组批量提交，保证原子性
//     —— 中途不会插入别的输入事件（这正是当年弃用 mouse_event 的原因之一）。
//
// 为什么继续用 SendInput 而不是 mouse_event：
//   mouse_event 已被微软标记为 deprecated，且无法一次原子提交组合输入。
// ============================================================

#include "input_sink.hpp"
#include "input_types.hpp"

#include <mutex>

namespace rc::server {

class InputExecutor final : public IInputSink {
public:
    InputExecutor()                                = default;
    ~InputExecutor() override                      = default;
    InputExecutor(const InputExecutor&)            = delete;
    InputExecutor& operator=(const InputExecutor&) = delete;

    void apply_mouse(const rc::input::MouseEvent& ev) override;
    void apply_keyboard(const rc::input::KeyboardEvent& ev) override;

private:
    /// 保护一次完整的注入序列（尤其是双击这种多步输入）。
    /// 注意：粒度是「一次事件」，而不是「一个会话」——不能让某个客户端的
    /// 大量移动事件把其他客户端饿死。
    std::mutex mutex_;
};

} // namespace rc::server
