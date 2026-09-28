// 实验 3：DPI 不感知的进程调用 SetCursorPos，坐标会被系统换算吗？
//
// 为什么值得单独验：服务端进程没有设 DPI 感知，因此它活在系统给的"虚拟桌面"
//   坐标系里（本机 150% 缩放 -> 2560x1440 被虚拟成 1707x960）。
//   已经实测确认：**GetCursorInfo 返回的是虚拟坐标**（光标物理在 (1689,90)，
//   服务端读到 (1126,60)，正好是 2/3）。
//   那么 SetCursorPos 呢？两者必须被同样换算，映射才自洽：
//     客户端在画面上点了 (x,y) -> 服务端 SetCursorPos(x,y)
//       - 若同样被换算：落到物理 (1.5x, 1.5y)，正是画面像素对应的真实位置 -> 正确
//       - 若没有被换算：落到物理 (x,y)，缩放屏上点右下角就会偏 -> 功能性缺陷
//
// 做法：本程序（不感知）把光标设到虚拟坐标 (400,300)，然后退出；
//   再用一个 DPI 感知的进程读物理位置：
//     读到 (600,450)  => 被换算，映射自洽
//     读到 (400,300)  => 没有被换算，说明书里的映射在缩放屏上是错的
#include <Windows.h>

#include <cstdio>

int main() {
    const int cx = ::GetSystemMetrics(SM_CXSCREEN);
    const int cy = ::GetSystemMetrics(SM_CYSCREEN);
    std::printf("本进程（DPI 不感知）看到的虚拟屏 = %dx%d\n", cx, cy);

    const int tx = 400;
    const int ty = 300;
    ::SetCursorPos(tx, ty);

    CURSORINFO ci{};
    ci.cbSize = sizeof(ci);
    ::GetCursorInfo(&ci);
    std::printf("SetCursorPos(%d,%d) -> 本进程读回 (%ld,%ld)\n", tx, ty,
                ci.ptScreenPos.x, ci.ptScreenPos.y);

    std::printf("\n接下来请用 DPI 感知的进程读一次物理位置，判读：\n");
    std::printf("  物理 (600,450) = 虚拟值 x1.5  => SetCursorPos 同样被换算，映射自洽\n");
    std::printf("  物理 (400,300) = 虚拟值原样   => 只有 Get 被换算，缩放屏上点击会偏\n");
    return 0;
}
