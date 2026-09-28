#pragma once
// ============================================================
// Win32 裸资源的 RAII 删除器集合
//
// 旧代码里 ReleaseDC / GlobalFree / IStream::Release 分散在多个
// return 路径上，任何一处提前 return 就是资源泄漏
// （旧版 HandleScreen 就存在双重释放的隐患：pStream->Release() 之后
//  又 GlobalFree(hMen)）。unique_ptr + 删除器让“忘记释放/重复释放”
// 在类型系统层面不可能发生。
// ============================================================

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <Windows.h>
#include <objidl.h>

#include <memory>

namespace rc::win {

// 注意每个删除器里的 `using pointer = ...`：这不是可选项。
//   std::unique_ptr<T, D> 的 pointer 默认是 T*。
//   HDC / HGLOBAL 本身就是指针类型（HDC__* / void*），
//   若直接写 std::unique_ptr<HDC, D> 而不指定 D::pointer，
//   推导出的 pointer 会是 HDC__**，编译期就会报“无法从 HDC 转换”。
//   显式声明 D::pointer 后，unique_ptr 才按“句柄”语义工作。

// GetDC(nullptr) 的配对释放器
struct ScreenDcReleaser {
    using pointer = HDC;
    void operator()(HDC dc) const {
        if (dc) ::ReleaseDC(nullptr, dc);
    }
};

// GlobalAlloc 的配对释放器
struct GlobalFreeReleaser {
    using pointer = HGLOBAL;
    void operator()(HGLOBAL h) const {
        if (h) ::GlobalFree(h);
    }
};

// IStream 的配对释放器（CreateStreamOnHGlobal 且 fDeleteOnRelease=TRUE 时，
// Release() 会连底层内存一起释放——此时不要再手工 GlobalFree！）
struct IStreamReleaser {
    using pointer = IStream*;
    void operator()(IStream* s) const {
        if (s) s->Release();
    }
};

// GDI 对象（位图/画笔/字体……）的配对释放器。
//
// 用途之一是 GetIconInfo：它每次都会新建两张位图（AND 掩码 + 彩色），
// 调用方必须自行 DeleteObject。抓屏是每帧都调用它的，
// 漏删就是每秒泄漏几十个 GDI 对象，几分钟后进程的 GDI 句柄配额
// （默认 10000）耗尽，抓屏与界面绘制会一起失败——而且现象离根因很远。
struct GdiObjectReleaser {
    using pointer = HGDIOBJ;
    void operator()(HGDIOBJ o) const {
        if (o) ::DeleteObject(o);
    }
};

// 面向调用方的别名：业务代码只写 rc::win::ScreenDcPtr，不再重复写删除器
using ScreenDcPtr  = std::unique_ptr<HDC, ScreenDcReleaser>;
using GlobalMemPtr = std::unique_ptr<HGLOBAL, GlobalFreeReleaser>;
using IStreamPtr   = std::unique_ptr<IStream, IStreamReleaser>;
using GdiObjectPtr = std::unique_ptr<HGDIOBJ, GdiObjectReleaser>;

} // namespace rc::win
