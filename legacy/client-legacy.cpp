#include<stdio.h>
#include<Windows.h>
#include<atlimage.h>
#pragma comment(lib,"ws2_32.lib")
#define RECV_BUFFER_LEN 1024*1024*10

// 将这个结构体按一字节对齐
#pragma pack(push,1)
struct PacketHeader {
	int magic;    //4字节包头
	int cmd;      //四字节命令号
	int body_len; //数据长度
};
#pragma pack(pop)

struct Packet {
	PacketHeader header;//包头
	char body[];        //包数据
};

enum ENUM_MOUSE {
	MOUSE_MOVE = 1, MOUSE_LDOWN = 2, MOUSE_LUP = 3,
	MOUSE_RDOWN = 4, MOUSE_RUP = 5, MOUSE_MDOWN = 6,
	MOUSE_MUP = 7, MOUSE_LCLICK = 8, MOUSE_RCLICK = 9,
	MOUSE_MCLICK = 10, MOUSE_LDCLICK = 11, MOUSE_RDCLICK = 12,
	MOUSE_MDCLICK = 13,
};

// ✅ 修复：网络传输结构体必须显式按1字节对齐
#pragma pack(push, 1)
struct Mouse {
	int action;
	POINT ptXY;
};

struct KeyBoard {
	int virtual_code;
	int key_status;
};
#pragma pack(pop)

enum CMD {
	CMD_SCREEN = 1,
	CMD_MOUSE = 2,
	CMD_KEYBOARD = 4,
	CMD_TESTCONNECT = 2026
};

Packet* PackPacket(int cmd, char* buffer, int buffer_len);
Packet* ParsePacket(char* buffer, int len);
int GetPacketLen(Packet* pck);
DWORD WINAPI SendScreenCallBack(LPVOID lpThreadParameter);
int InitSocket();

SOCKET g_server_socket;
SOCKADDR_IN g_server_addr;
HWND g_hwnd = NULL;
CImage g_image;
int g_remote_height = -1;
int g_remote_width = -1;

CRITICAL_SECTION g_cri_sec;
bool g_is_running = true; // ✅ 修复：增加线程运行标志，方便优雅退出

LRESULT CALLBACK winProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
	switch (msg) {
	case WM_PAINT: {
		PAINTSTRUCT ps;
		HDC hdc = BeginPaint(hwnd, &ps);

		// ✅ 修复：错误使用临界区导致死锁。正确包裹整个 g_image 的使用过程。
		EnterCriticalSection(&g_cri_sec);
		if (!g_image.IsNull()) {
			RECT client_rect;
			GetClientRect(hwnd, &client_rect);
			int client_width = client_rect.right - client_rect.left;
			int client_height = client_rect.bottom - client_rect.top;

			int oldMode = SetStretchBltMode(hdc, HALFTONE);
			SetBrushOrgEx(hdc, 0, 0, NULL);

			int remote_width = g_image.GetWidth();
			int remote_height = g_image.GetHeight();
			g_image.StretchBlt(hdc, 0, 0, client_width, client_height, 0, 0, remote_width, remote_height, SRCCOPY);

			SetStretchBltMode(hdc, oldMode);
		}
		LeaveCriticalSection(&g_cri_sec); // ✅ 修复：确保 Enter 和 Leave 成对出现

		EndPaint(hwnd, &ps);
	}
				 break;
	case WM_MOUSEMOVE: {
		int xPos = LOWORD(lParam);
		int yPos = HIWORD(lParam);
		RECT client_rect;
		GetClientRect(hwnd, &client_rect);
		int client_width = client_rect.right - client_rect.left;
		int client_height = client_rect.bottom - client_rect.top;
		if (g_remote_width != -1 && g_remote_height != -1 && client_width > 0 && client_height > 0) {

			// ✅ 修复：增加时间节流，防止鼠标移动事件发送过于频繁导致网络洪泛和卡顿。
			// 建议 16ms 发送一次（约60帧/秒）。
			static DWORD last_move_time = 0;
			DWORD now = GetTickCount();
			if (now - last_move_time > 16) {
				last_move_time = now;

				int rxPox = xPos * g_remote_width / client_width;
				int ryPox = yPos * g_remote_height / client_height;

				Mouse mouse;
				mouse.action = MOUSE_MOVE;
				mouse.ptXY.x = rxPox;
				mouse.ptXY.y = ryPox;
				Packet* packet = PackPacket(CMD_MOUSE, (char*)&mouse, sizeof(Mouse)); // ✅ 修复：发送整个mouse结构体，而不是只发action
				send(g_server_socket, (char*)&packet->header.magic, GetPacketLen(packet), 0);

				// ✅ 修复：删除高频的 OutputDebugString，会导致程序卡死
				free(packet);
			}
		}
	}
					 break;
					 // ... 鼠标其他按键操作代码不变，只需注意检查宽高和释放内存 ...
	case WM_LBUTTONDOWN: {
		// 代码省略，逻辑同上，但要把 mouse.action = MOUSE_LDOWN;
		// ✅ 修复：发送时确保打包的是整个结构体
		int xPos = LOWORD(lParam); int yPos = HIWORD(lParam);
		RECT client_rect; GetClientRect(hwnd, &client_rect);
		int client_width = client_rect.right - client_rect.left; int client_height = client_rect.bottom - client_rect.top;
		if (g_remote_width != -1 && g_remote_height != -1 && client_width > 0 && client_height > 0) {
			Mouse mouse;
			mouse.action = MOUSE_LDOWN;
			mouse.ptXY.x = xPos * g_remote_width / client_width;
			mouse.ptXY.y = yPos * g_remote_height / client_height;
			Packet* packet = PackPacket(CMD_MOUSE, (char*)&mouse, sizeof(Mouse));
			send(g_server_socket, (char*)&packet->header.magic, GetPacketLen(packet), 0);
			free(packet);
		}
	} break;
	case WM_LBUTTONUP: { // ✅ 补充：左键抬起
		int xPos = LOWORD(lParam); int yPos = HIWORD(lParam);
		RECT client_rect; GetClientRect(hwnd, &client_rect);
		int client_width = client_rect.right - client_rect.left; int client_height = client_rect.bottom - client_rect.top;
		if (g_remote_width != -1 && g_remote_height != -1 && client_width > 0 && client_height > 0) {
			Mouse mouse;
			mouse.action = MOUSE_LUP; // 注意这里是 LUP
			mouse.ptXY.x = xPos * g_remote_width / client_width;
			mouse.ptXY.y = yPos * g_remote_height / client_height;
			Packet* packet = PackPacket(CMD_MOUSE, (char*)&mouse, sizeof(Mouse));
			send(g_server_socket, (char*)&packet->header.magic, GetPacketLen(packet), 0);
			free(packet);
		}
	} break;
	case WM_RBUTTONDOWN: { // ✅ 补充：右键按下
		int xPos = LOWORD(lParam); int yPos = HIWORD(lParam);
		RECT client_rect; GetClientRect(hwnd, &client_rect);
		int client_width = client_rect.right - client_rect.left; int client_height = client_rect.bottom - client_rect.top;
		if (g_remote_width != -1 && g_remote_height != -1 && client_width > 0 && client_height > 0) {
			Mouse mouse;
			mouse.action = MOUSE_RDOWN;
			mouse.ptXY.x = xPos * g_remote_width / client_width;
			mouse.ptXY.y = yPos * g_remote_height / client_height;
			Packet* packet = PackPacket(CMD_MOUSE, (char*)&mouse, sizeof(Mouse));
			send(g_server_socket, (char*)&packet->header.magic, GetPacketLen(packet), 0);
			free(packet);
		}
	} break;
	case WM_RBUTTONUP: { // ✅ 补充：右键抬起
		int xPos = LOWORD(lParam); int yPos = HIWORD(lParam);
		RECT client_rect; GetClientRect(hwnd, &client_rect);
		int client_width = client_rect.right - client_rect.left; int client_height = client_rect.bottom - client_rect.top;
		if (g_remote_width != -1 && g_remote_height != -1 && client_width > 0 && client_height > 0) {
			Mouse mouse;
			mouse.action = MOUSE_RUP;
			mouse.ptXY.x = xPos * g_remote_width / client_width;
			mouse.ptXY.y = yPos * g_remote_height / client_height;
			Packet* packet = PackPacket(CMD_MOUSE, (char*)&mouse, sizeof(Mouse));
			send(g_server_socket, (char*)&packet->header.magic, GetPacketLen(packet), 0);
			free(packet);
		}
	} break;
	case WM_LBUTTONDBLCLK: { // ✅ 补充：左键双击
		int xPos = LOWORD(lParam); int yPos = HIWORD(lParam);
		RECT client_rect; GetClientRect(hwnd, &client_rect);
		int client_width = client_rect.right - client_rect.left; int client_height = client_rect.bottom - client_rect.top;
		if (g_remote_width != -1 && g_remote_height != -1 && client_width > 0 && client_height > 0) {
			Mouse mouse;
			mouse.action = MOUSE_LDCLICK;
			mouse.ptXY.x = xPos * g_remote_width / client_width;
			mouse.ptXY.y = yPos * g_remote_height / client_height;
			Packet* packet = PackPacket(CMD_MOUSE, (char*)&mouse, sizeof(Mouse));
			send(g_server_socket, (char*)&packet->header.magic, GetPacketLen(packet), 0);
			free(packet);
		}
	} break;

						 // 可继续补充 WM_MBUTTONDOWN, WM_MBUTTONUP 等
					   // 其他鼠标事件（WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONDOWN等）请参照上面修改，确保发送 sizeof(Mouse)

	case WM_KEYDOWN:

	case WM_SYSKEYDOWN: {
		KeyBoard key_board;
		key_board.virtual_code = wParam;
		key_board.key_status = 0;
		Packet* packet = PackPacket(CMD_KEYBOARD, (char*)&key_board, sizeof(KeyBoard)); // ✅ 修复：发送结构体，而不是仅发送第一个成员
		send(g_server_socket, (char*)&packet->header.magic, GetPacketLen(packet), 0);
		free(packet);
	} break;
	case WM_KEYUP:
	case WM_SYSKEYUP: {
		KeyBoard key_board;
		key_board.virtual_code = wParam;
		key_board.key_status = 1; // 注意这里是 1，代表松开
		Packet* packet = PackPacket(CMD_KEYBOARD, (char*)&key_board, sizeof(KeyBoard));
		send(g_server_socket, (char*)&packet->header.magic, GetPacketLen(packet), 0);
		free(packet);
	} break;
	case WM_DESTROY: // ✅ 修复：窗口关闭时，通知线程退出，释放资源
		g_is_running = false;
		closesocket(g_server_socket); // 强制 recv 返回，解除阻塞
		PostQuitMessage(0);
		break;
	default:
		return DefWindowProc(hwnd, msg, wParam, lParam);
	}
	return 0;
}
int InitWindow(HINSTANCE hInstance, int nCmdshow);
// WinMain 保持不变，但建议增加 WSACleanup 和 DeleteCriticalSection
int WINAPI WinMain(HINSTANCE hInstance, HINSTANCE hPreventInstance, PSTR pCmdLine, int nCmdshow) {
	InitializeCriticalSection(&g_cri_sec);
	InitWindow(hInstance, nCmdshow);
	if (InitSocket() == 0) {
		MessageBox(NULL, "Socket 初始化失败", "错误", MB_OK | MB_ICONERROR);
		return 0;
	}

	if (connect(g_server_socket, (sockaddr*)&g_server_addr, sizeof(SOCKADDR_IN)) == SOCKET_ERROR) {
		printf("连接服务器失败\r\n");
		return 0;
	}

	unsigned long send_screen_thread_id = 0;
	HANDLE handle_send_screen = CreateThread(NULL, 0, SendScreenCallBack, NULL, 0, &send_screen_thread_id);
	OutputDebugString("连接服务器成功\r\n");

	MSG msg = { 0 };
	while (GetMessage(&msg, NULL, 0, 0)) {
		TranslateMessage(&msg);
		DispatchMessage(&msg);
	}

	// ✅ 修复：等待线程结束并清理资源
	WaitForSingleObject(handle_send_screen, 1000);
	CloseHandle(handle_send_screen);
	DeleteCriticalSection(&g_cri_sec);
	closesocket(g_server_socket);
	WSACleanup();
	return 0;
}

DWORD WINAPI SendScreenCallBack(LPVOID lpThreadParameter) {
	char* recv_buffer = (char*)malloc(RECV_BUFFER_LEN);
	while (g_is_running) {
		Packet* pack = PackPacket(CMD_SCREEN, NULL, 0);
		int sen_len = send(g_server_socket, (char*)&pack->header.magic, GetPacketLen(pack), 0);
		if (sen_len <= 0) { free(pack); break; } // ✅ 修复：发送失败直接退出
		free(pack);

		// 注意：这里的 recv 会阻塞，直到收到数据或 socket 关闭
		int len = recv(g_server_socket, recv_buffer, RECV_BUFFER_LEN, 0);
		if (len > 0) {
			Packet* pack = ParsePacket(recv_buffer, len);
			if (pack != NULL) {
				HGLOBAL hMen = GlobalAlloc(GMEM_MOVEABLE, 0);
				if (hMen == NULL) { free(pack); continue; }
				IStream* pStream = NULL;
				HRESULT ret = CreateStreamOnHGlobal(hMen, true, &pStream);
				if (ret == S_OK) {
					ULONG length = 0;
					// ✅ 修复：必须在 Write 之后再 free(pack)，否则是 Use-After-Free
					pStream->Write(pack->body, pack->header.body_len, &length);

					LARGE_INTEGER lg = { 0 };
					pStream->Seek(lg, STREAM_SEEK_SET, NULL);

					EnterCriticalSection(&g_cri_sec);
					if (!g_image.IsNull()) g_image.Destroy();
					g_image.Load(pStream);
					if (g_remote_width == -1 && g_remote_height == -1) {
						g_remote_width = g_image.GetWidth();
						g_remote_height = g_image.GetHeight();
					}
					LeaveCriticalSection(&g_cri_sec);

					InvalidateRect(g_hwnd, NULL, FALSE);
					UpdateWindow(g_hwnd);
				}
				// ✅ 修复：无论 pStream 是否创建成功，都要释放
				if (pStream) pStream->Release();
				GlobalFree(hMen);
				free(pack); // ✅ 移到这里，安全释放
			}
		}
		else {
			break; // 连接断开，退出线程
		}
	}
	free(recv_buffer);
	return 0;
}

// ✅ 修复：完善 GetPacketLen 缺省返回值
int GetPacketLen(Packet* pck) {
	if (pck != NULL) return pck->header.body_len + sizeof(PacketHeader);
	return 0;
}


int InitSocket() {
	WSADATA wsadta;
	WSAStartup(MAKEWORD(2, 2), &wsadta);
	g_server_socket = socket(AF_INET, SOCK_STREAM, 0);
	if (g_server_socket == INVALID_SOCKET) return 0;
	g_server_addr.sin_family = AF_INET;
	g_server_addr.sin_port = htons(9999);
	g_server_addr.sin_addr.S_un.S_addr = inet_addr("127.0.0.1");
	return 1;
}
// 初始化窗口的具体实现
int InitWindow(HINSTANCE hInstance, int nCmdshow) {
	WNDCLASS ws = {};
	LPCSTR CLASS_NAME = "MainWindow";
	ws.lpfnWndProc = winProc; // 窗口消息的处理函数
	ws.hInstance = hInstance; // 实例句柄
	ws.lpszClassName = CLASS_NAME;
	ws.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
	ws.hCursor = LoadCursor(NULL, IDC_ARROW); // 光标
	ws.hIcon = LoadIconA(NULL, IDI_APPLICATION); // 图标
	ws.style = CS_HREDRAW | CS_VREDRAW; // 窗口大小变化时重绘

	if (!RegisterClass(&ws)) {
		MessageBox(NULL, "窗口注册失败", "错误", MB_OK | MB_ICONERROR);
		return 0;
	}

	g_hwnd = CreateWindow(
		CLASS_NAME,
		"远程控制",
		WS_OVERLAPPEDWINDOW,
		CW_USEDEFAULT, CW_USEDEFAULT,
		600, 400,
		NULL, NULL, hInstance, NULL
	);

	if (g_hwnd == NULL) {
		MessageBox(NULL, "窗口创建失败", "错误", MB_OK | MB_ICONERROR);
		return 0;
	}

	ShowWindow(g_hwnd, nCmdshow);
	UpdateWindow(g_hwnd);
	return 0;
}

// ✅ 替换 client.cpp 底部的 ParsePacket 为以下内容
Packet* ParsePacket(char* buffer, int len) {
	int index = 0;
	// 找包头
	while (index <= len - 4) {
		if (*(int*)(buffer + index) == 0x55AA77CC) {
			break;
		}
		index++;
	}
	if (index > len - 4) return NULL; // 没找到包头

	index += 4;
	if (index + 4 > len) return NULL; // ✅ cmd 不完整
	int cmd = *(int*)(buffer + index); index += 4;

	if (index + 4 > len) return NULL; // ✅ body_len 不完整
	int body_len = *(int*)(buffer + index); index += 4;

	if (index + body_len > len) return NULL; // ✅ body 不完整（半包）

	Packet* ppck = (Packet*)malloc(sizeof(PacketHeader) + body_len);
	ppck->header.magic = 0x55AA77CC;
	ppck->header.cmd = cmd;
	ppck->header.body_len = body_len;
	if (body_len > 0) {
		memcpy(ppck->body, buffer + index, body_len);
	}
	return ppck;
}
// ✅ 补全丢失的 PackPacket 函数（放在 ParsePacket 下方）
Packet* PackPacket(int cmd, char* buffer, int buffer_len) {
	Packet* pck = (Packet*)malloc(buffer_len + sizeof(PacketHeader));
	pck->header.magic = 0x55AA77CC;
	pck->header.cmd = cmd;
	pck->header.body_len = buffer_len;
	if (buffer_len > 0 && buffer != NULL) {
		memcpy(pck->body, buffer, buffer_len);
	}
	return pck;
}