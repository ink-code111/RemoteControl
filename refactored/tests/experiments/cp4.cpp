// 实验 4：客户端窗口里的"光标不同步"到底怎么来的？
//
// 两个候选解释，需要用数据分开：
//   (A) 显示比例问题 —— 客户端把整屏画面 StretchBlt 铺满自己的客户区，
//       合成进画面的光标因此落在"缩放后"的位置；而 Windows 把真实光标
//       按 1:1 画在窗口上。于是同一个窗口里出现两个位置不同的光标。
//   (B) 输入回灌 —— 客户端把 WM_MOUSEMOVE 映射后发给服务端，
//       服务端 SetCursorPos。同机自测时这等于"自己把自己的光标挪走"，
//       而且挪完会产生新的 WM_MOUSEMOVE，形成闭环。
//
// 用法：
//   cp4.exe report                     打印物理屏 / 当前光标 / 客户端窗口矩形
//   cp4.exe watch  <x> <y> [ms]        把光标钉到物理 (x,y)，随后每 20ms 采一次，
//                                      看它会不会被别的进程（客户端回灌）拖走
//
// 判读：
//   watch 里位置一直不变        -> 没有回灌闭环，问题只在 (A)
//   watch 里位置单调跑偏        -> (B) 成立：闭环在把光标推走
#define _CRT_SECURE_NO_WARNINGS
#include <Windows.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

const wchar_t* kClientClass = L"RcRemoteWindow";

void report_cursor(const char* tag) {
    POINT p{};
    ::GetCursorPos(&p);
    std::printf("  [%s] GetCursorPos = (%ld, %ld)\n", tag, p.x, p.y);
}

void dump_window() {
    HWND h = ::FindWindowW(kClientClass, nullptr);
    if (h == nullptr) {
        std::printf("  客户端窗口（类名 %ls）未找到 —— 客户端还在跑吗？\n", kClientClass);
        return;
    }

    RECT wr{};
    RECT cr{};
    ::GetWindowRect(h, &wr);
    ::GetClientRect(h, &cr);

    POINT origin{0, 0};
    ::ClientToScreen(h, &origin);

    const int cw = cr.right - cr.left;
    const int ch = cr.bottom - cr.top;

    std::printf("  窗口句柄 = %p\n", static_cast<void*>(h));
    std::printf("  窗口矩形（含边框标题）= [%ld, %ld, %ld, %ld]  %ldx%ld\n",
                wr.left, wr.top, wr.right, wr.bottom, wr.right - wr.left, wr.bottom - wr.top);
    std::printf("  客户区原点（屏幕坐标）= (%ld, %ld)\n", origin.x, origin.y);
    std::printf("  客户区尺寸             = %dx%d\n", cw, ch);
}

/// 钉住光标后连续采样，看有没有别的进程在把它挪走
void watch(int x, int y, int ms) {
    // 先把客户端窗口提到前台：否则光标落在别的窗口上时，
    // WM_MOUSEMOVE 会发给那个窗口，客户端根本收不到，测不出回灌
    HWND h = ::FindWindowW(kClientClass, nullptr);
    if (h != nullptr) {
        ::ShowWindow(h, SW_RESTORE);
        ::SetForegroundWindow(h);
        ::Sleep(120);
        std::printf("  已把客户端窗口提到前台\n");
    }

    ::SetCursorPos(x, y);
    const int steps = ms / 20;
    POINT prev{};
    ::GetCursorPos(&prev);
    std::printf("  SetCursorPos(%d, %d) -> 读回 (%ld, %ld)\n", x, y, prev.x, prev.y);

    int moved = 0;
    for (int i = 0; i < steps; ++i) {
        ::Sleep(20);
        POINT p{};
        ::GetCursorPos(&p);
        if (p.x != prev.x || p.y != prev.y) {
            ++moved;
            std::printf("   +%4d ms -> (%ld, %ld)   <== 被外部改动\n", (i + 1) * 20, p.x, p.y);
            prev = p;
        }
    }
    std::printf("\n  采样 %d 次，其中 %d 次位置发生变化\n", steps, moved);
    std::printf("  判读：%s\n",
                moved == 0 ? "位置稳定 —— 没有回灌闭环"
                           : "光标被持续改动 —— 同机回灌闭环成立（客户端在 SetCursorPos 自己的光标）");
}

} // namespace

int main(int argc, char** argv) {
    // 必须 DPI 感知：否则读到的全是虚拟坐标（1707x960），和物理屏对不上
    ::SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

    const char* mode = (argc > 1) ? argv[1] : "report";

    if (std::strcmp(mode, "watch") == 0 && argc >= 4) {
        const int x  = std::atoi(argv[2]);
        const int y  = std::atoi(argv[3]);
        const int ms = (argc > 4) ? std::atoi(argv[4]) : 1200;
        std::printf("=== watch：钉住 (%d, %d) 观察 %d ms ===\n", x, y, ms);
        watch(x, y, ms);
        return 0;
    }

    std::printf("=== report（本进程 DPI 感知） ===\n");
    std::printf("  物理屏 = %dx%d\n",
                ::GetSystemMetrics(SM_CXSCREEN), ::GetSystemMetrics(SM_CYSCREEN));
    report_cursor("当前");
    dump_window();
    return 0;
}
