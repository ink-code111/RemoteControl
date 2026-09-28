#include "input_executor.hpp"
#include "logger.hpp"

#include <array>

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <Windows.h>

#include <string>

namespace rc::server {
namespace {

/// 提交一组输入事件。SendInput 返回实际插入的事件数，
/// 数量不符说明被 UIPI 拦了（例如目标窗口以管理员权限运行，当前进程权限不够）。
void send_inputs(const INPUT* inputs, UINT count, const char* what) {
    const UINT sent = ::SendInput(count, const_cast<INPUT*>(inputs), sizeof(INPUT));
    if (sent != count) {
        RC_LOG_WARN("SendInput({}) inserted {}/{} events, GetLastError={}",
                    what, sent, count, ::GetLastError());
    }
}

INPUT make_mouse_input(DWORD flags, LONG dx = 0, LONG dy = 0, DWORD mouse_data = 0) {
    INPUT in{};
    in.type           = INPUT_MOUSE;
    in.mi.dx          = dx;
    in.mi.dy          = dy;
    in.mi.mouseData   = mouse_data;
    in.mi.dwFlags     = flags;
    return in;
}

/// 单个鼠标动作
void send_mouse_flag(DWORD flags, const char* what) {
    const INPUT in = make_mouse_input(flags);
    send_inputs(&in, 1, what);
}

/// 双击：一次提交"按下-抬起-按下-抬起"，保证四步之间不会插入其他输入
void send_double_click(DWORD down_flag, DWORD up_flag, const char* what) {
    std::array<INPUT, 4> seq{
        make_mouse_input(down_flag),
        make_mouse_input(up_flag),
        make_mouse_input(down_flag),
        make_mouse_input(up_flag),
    };
    send_inputs(seq.data(), static_cast<UINT>(seq.size()), what);
}

} // namespace

const char* to_string(rc::input::MouseAction a) noexcept {
    switch (a) {
    case rc::input::MouseAction::kMove:    return "move";
    case rc::input::MouseAction::kLDown:   return "l_down";
    case rc::input::MouseAction::kLUp:     return "l_up";
    case rc::input::MouseAction::kRDown:   return "r_down";
    case rc::input::MouseAction::kRUp:     return "r_up";
    case rc::input::MouseAction::kMDown:   return "m_down";
    case rc::input::MouseAction::kMUp:     return "m_up";
    case rc::input::MouseAction::kLDClick: return "l_dclick";
    case rc::input::MouseAction::kRDClick: return "r_dclick";
    case rc::input::MouseAction::kMDClick: return "m_dclick";
    case rc::input::MouseAction::kWheel:   return "wheel";
    }
    return "unknown";
}

void InputExecutor::apply_mouse(const rc::input::MouseEvent& ev) {
    std::lock_guard<std::mutex> lock(mutex_);
    switch (ev.action) {
    case rc::input::MouseAction::kMove:
        // 与旧版一致：绝对定位走 SetCursorPos（不产生 MOUSEEVENTF_MOVE 的相对位移语义问题）
        ::SetCursorPos(ev.x, ev.y);
        break;
    case rc::input::MouseAction::kLDown:
        send_mouse_flag(MOUSEEVENTF_LEFTDOWN, "l_down");
        break;
    case rc::input::MouseAction::kLUp:
        send_mouse_flag(MOUSEEVENTF_LEFTUP, "l_up");
        break;
    case rc::input::MouseAction::kRDown:
        send_mouse_flag(MOUSEEVENTF_RIGHTDOWN, "r_down");
        break;
    case rc::input::MouseAction::kRUp:
        send_mouse_flag(MOUSEEVENTF_RIGHTUP, "r_up");
        break;
    case rc::input::MouseAction::kMDown:
        send_mouse_flag(MOUSEEVENTF_MIDDLEDOWN, "m_down");
        break;
    case rc::input::MouseAction::kMUp:
        send_mouse_flag(MOUSEEVENTF_MIDDLEUP, "m_up");
        break;
    case rc::input::MouseAction::kLDClick:
        send_double_click(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, "l_dclick");
        break;
    case rc::input::MouseAction::kRDClick:
        send_double_click(MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, "r_dclick");
        break;
    case rc::input::MouseAction::kMDClick:
        send_double_click(MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, "m_dclick");
        break;
    case rc::input::MouseAction::kWheel: {
        // 滚轮需要先定位再滚动，否则会在错误的位置生效
        ::SetCursorPos(ev.x, ev.y);
        const INPUT in = make_mouse_input(MOUSEEVENTF_WHEEL, 0, 0,
                                          static_cast<DWORD>(ev.wheel_delta));
        send_inputs(&in, 1, "wheel");
        break;
    }
    }
}

void InputExecutor::apply_keyboard(const rc::input::KeyboardEvent& ev) {
    std::lock_guard<std::mutex> lock(mutex_);
    INPUT in{};
    in.type       = INPUT_KEYBOARD;
    in.ki.wVk     = static_cast<WORD>(ev.vk);
    in.ki.wScan   = 0;
    in.ki.dwFlags = ev.up ? KEYEVENTF_KEYUP : 0; // 与旧版语义一致：0=按下 1=抬起
    in.ki.time    = 0;
    send_inputs(&in, 1, "keyboard");
}

} // namespace rc::server
