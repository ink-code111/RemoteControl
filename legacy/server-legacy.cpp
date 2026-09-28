#include<stdio.h>
#include<Windows.h>
#include<atlimage.h>
#include<ShellScalingApi.h>
#pragma comment(lib,"ws2_32.lib")
#define RECV_BUFFER_SIZE 1024 * 1024 * 10 // ✅ 提高缓冲区大小

#pragma pack(push,1)
struct PacketHeader {
	int magic;
	int cmd;
	int body_len;
};
#pragma pack(pop)

struct Packet {
	PacketHeader header;
	char body[];
};

// ✅ 修复：网络传输结构体显式按1字节对齐
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
	CMD_SCREEN = 1, CMD_MOUSE = 2, CMD_KEYBOARD = 4, CMD_TESTCONNECT = 2026
};

enum ENUM_MOUSE {
	MOUSE_MOVE = 1, MOUSE_LDOWN = 2, MOUSE_LUP = 3, MOUSE_RDOWN = 4, MOUSE_RUP = 5,
	MOUSE_MDOWN = 6, MOUSE_MUP = 7, MOUSE_LCLICK = 8, MOUSE_RCLICK = 9, MOUSE_MCLICK = 10,
	MOUSE_LDCLICK = 11, MOUSE_RDCLICK = 12, MOUSE_MDCLICK = 13,
};

Packet* ParsePacket(char* buffer, int len);
Packet* PackPacket(int cmd, char* buffer, int buffer_len);
int GetPacketLen(Packet* pck);

int InitServer();
int HandleCommand(Packet* packet);
int HandleScreen(Packet* packet);
int HandleMouse(Packet* packet);
int HandlekeyBoard(Packet* packet);
int HandleTestConnect(Packet* packet);

SOCKET g_server_socket;
SOCKET g_client_socket;
unsigned long g_screen_thread_id = 0;
unsigned long g_mouse_thread_id = 0;
unsigned long g_keybroad_thread_id = 0;

#define WM_HANDLE_SCREEN (WM_USER+1)
#define WM_HANDLE_MOUSE (WM_USER+2)
#define WM_HANDLE_KEYBROAD (WM_USER+3)
#define WM_HANDLE_INVOKE_MSG_LOOP (WM_USER+4)

DWORD WINAPI HandleScreenThreadFuc(LPVOID lpThreadParameter) {
	MSG msg;
	while (GetMessage(&msg, 0, 0, 0)) {
		if (msg.message == WM_HANDLE_SCREEN) {
			Packet* packet = (Packet*)msg.lParam;
			int result = HandleScreen(packet);
			free(packet);
			if (result == -1) { // ✅ 如果发送失败（客户端断开），退出线程
				printf("客户端已断开，屏幕发送线程退出\r\n");
				break;
			}
		}
	}
	return 0;
}
DWORD WINAPI HandleMouseThreadFuc(LPVOID lpThreadParameter) {
	MSG msg;
	while (GetMessage(&msg, 0, 0, 0)) {
		if (msg.message == WM_HANDLE_MOUSE) {
			Packet* packet = (Packet*)msg.lParam;
			HandleMouse(packet);
			free(packet);
		}
	}
	return 0;
}
DWORD WINAPI HandleKeyBroadThreadFuc(LPVOID lpThreadParameter) {
	MSG msg;
	while (GetMessage(&msg, 0, 0, 0)) {
		if (msg.message == WM_HANDLE_KEYBROAD) {
			Packet* packet = (Packet*)msg.lParam;
			HandlekeyBoard(packet);
			free(packet);
		}
	}
	return 0;
}

int main() {
	// ✅ 修复：DPI感知只需在程序启动时设置一次，不要放在循环或单次处理函数中
	SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

	if (InitServer() != 0) {
		printf("启动服务失败\r\n");
		return 0;
	}

	CreateThread(NULL, 0, HandleScreenThreadFuc, NULL, 0, &g_screen_thread_id);
	CreateThread(NULL, 0, HandleMouseThreadFuc, NULL, 0, &g_mouse_thread_id);
	CreateThread(NULL, 0, HandleKeyBroadThreadFuc, NULL, 0, &g_keybroad_thread_id);

	PostThreadMessage(g_screen_thread_id, WM_HANDLE_INVOKE_MSG_LOOP, NULL, NULL);
	PostThreadMessage(g_mouse_thread_id, WM_HANDLE_INVOKE_MSG_LOOP, NULL, NULL);
	PostThreadMessage(g_keybroad_thread_id, WM_HANDLE_INVOKE_MSG_LOOP, NULL, NULL);
	Sleep(100);

	SOCKADDR_IN client_addr;
	int client_addr_len = sizeof(SOCKADDR_IN);
	printf("等待客户端连接\r\n");
	g_client_socket = accept(g_server_socket, (sockaddr*)&client_addr, &client_addr_len);
	printf("客户端连接成功\r\n");

	char* buffer = (char*)malloc(RECV_BUFFER_SIZE);
	if (buffer == nullptr) { // ✅ 加上 malloc 检查
		printf("内存分配失败\r\n");
		closesocket(g_client_socket); closesocket(g_server_socket); WSACleanup(); return -1;
	}
	int index = 0;

	while (true) {
		// ✅ 修复：如果真的快满了，说明数据异常或包过大，直接丢弃重置并报警
		if (index >= RECV_BUFFER_SIZE - 1024) {
			printf("警告：接收缓冲区溢出，强制重置\r\n");
			index = 0;
		}

		printf("等待接受数据\r\n");
		int len = recv(g_client_socket, buffer + index, RECV_BUFFER_SIZE - index, 0);

		if (len > 0) {
			index += len;
			printf("接收数据成功:%d\r\n", len);
		}
		else {
			// ✅ 修复：处理客户端断开 (len == 0) 或网络错误 (len < 0)，防止死循环
			printf("客户端断开连接或接收失败\r\n");
			break;
		}

		if (index > 0) {
			Packet* packet = ParsePacket(buffer, index);
			while (packet != NULL && index > 0) {
				int packet_len = GetPacketLen(packet);

				// ✅ 修复：先移动缓冲区，再处理，防止解析越界
				index -= packet_len;
				memmove(buffer, buffer + packet_len, index);

				HandleCommand(packet); // HandleCommand 内部负责 free
				packet = ParsePacket(buffer, index);
			}
		}
	}

	free(buffer);
	closesocket(g_client_socket);
	closesocket(g_server_socket);
	WSACleanup();
	return 0;
}

int HandleCommand(Packet* packet) {
	printf("Handle cmd:%d\r\n", packet->header.cmd);
	switch (packet->header.cmd) {
	case CMD_SCREEN:
		// ✅ 修复：投递到线程后，主线程不再处理，所有权转移给屏幕线程。
		// 不要在此处调用 HandleScreen 或 free。
		PostThreadMessage(g_screen_thread_id, WM_HANDLE_SCREEN, NULL, (LPARAM)packet);
		break;
	case CMD_MOUSE:
		PostThreadMessage(g_mouse_thread_id, WM_HANDLE_MOUSE, NULL, (LPARAM)packet);
		break;
	case CMD_KEYBOARD:
		PostThreadMessage(g_keybroad_thread_id, WM_HANDLE_KEYBROAD, NULL, (LPARAM)packet);
		break;
	case CMD_TESTCONNECT:
		HandleTestConnect(packet);
		free(packet); // ✅ 修复：直接处理的命令，在此处释放内存
		break;
	default:
		free(packet); // ✅ 修复：未知命令，立即释放
		break;
	}
	return 0;
}

int HandleScreen(Packet* packet) {
	CImage image;
	HDC hScreen = GetDC(NULL);
	int bitWidth = GetDeviceCaps(hScreen, BITSPIXEL);

	int sWidth = GetSystemMetrics(SM_CXSCREEN);
	int sHeight = GetSystemMetrics(SM_CYSCREEN);
	printf("width:%d  height:%d\r\n", sWidth, sHeight);

	image.Create(sWidth, sHeight, bitWidth);
	BitBlt(image.GetDC(), 0, 0, sWidth, sHeight, hScreen, 0, 0, SRCCOPY);
	ReleaseDC(NULL, hScreen);

	HGLOBAL hMen = GlobalAlloc(GMEM_MOVEABLE, 0);
	if (hMen == NULL) return -1;

	IStream* pStream = NULL;
	HRESULT ret = CreateStreamOnHGlobal(hMen, true, &pStream);
	if (ret == S_OK) {
		image.Save(pStream, ::Gdiplus::ImageFormatPNG);
		LARGE_INTEGER lg = { 0 };
		pStream->Seek(lg, STREAM_SEEK_SET, NULL);

		char* pdata = (char*)GlobalLock(hMen);
		int len = (int)GlobalSize(hMen);

		// ✅ 修复：重命名局部变量，避免与函数参数 packet 重名
		Packet* send_packet = PackPacket(CMD_SCREEN, pdata, len);
		int send_len = send(g_client_socket, (char*)&send_packet->header.magic, GetPacketLen(send_packet), 0);

		if (send_len > 0) {
			printf("发送屏幕成功：%d\r\n", send_len);
		}
		else {
			printf("发送屏幕失败：%d，可能客户端已断开\r\n", send_len);
			free(send_packet); // 记得释放内存
			GlobalUnlock(hMen);
			if (pStream) pStream->Release();
			GlobalFree(hMen);
			image.ReleaseDC();
			return -1; // ✅ 返回错误，让上层线程决定是否退出
		}

		free(send_packet);
		GlobalUnlock(hMen);
	}

	if (pStream) pStream->Release();
	GlobalFree(hMen);
	image.ReleaseDC();
	return 0;
}

int HandleMouse(Packet* packet) {
	Mouse mouse;
	// ✅ 修复：确保只拷贝结构体大小的数据，防止越界
	int copy_len = (packet->header.body_len < sizeof(Mouse)) ? packet->header.body_len : sizeof(Mouse);
	memcpy(&mouse, packet->body, copy_len);

	printf("x=%d  y=%d action=%d\r\n", mouse.ptXY.x, mouse.ptXY.y, mouse.action);

	switch (mouse.action) {
	case MOUSE_MOVE:
		SetCursorPos(mouse.ptXY.x, mouse.ptXY.y);
		break;
	case MOUSE_LDOWN:
		mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, GetMessageExtraInfo());
		break;
	case MOUSE_LUP:
		mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, GetMessageExtraInfo());
		break;
	case MOUSE_RDOWN:
		mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, GetMessageExtraInfo());
		break;
	case MOUSE_RUP:
		mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, GetMessageExtraInfo());
		break;
	case MOUSE_MDOWN:
		mouse_event(MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, GetMessageExtraInfo());
		break;
	case MOUSE_MUP:
		mouse_event(MOUSEEVENTF_MIDDLEUP, 0, 0, 0, GetMessageExtraInfo());
		break;
	case MOUSE_LDCLICK:
		mouse_event(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_LEFTUP, 0, 0, 0, GetMessageExtraInfo());
		mouse_event(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_LEFTUP, 0, 0, 0, GetMessageExtraInfo());
		break;
	default:
		printf("未知鼠标操作：%d\r\n", mouse.action);
		break;
	}
	return 0;
}

int HandlekeyBoard(Packet* packet) {
	KeyBoard key_board;
	int copy_len = (packet->header.body_len < sizeof(KeyBoard)) ? packet->header.body_len : sizeof(KeyBoard);
	memcpy(&key_board, packet->body, copy_len);

	INPUT input = { 0 };
	input.type = INPUT_KEYBOARD;
	input.ki.wVk = key_board.virtual_code;
	input.ki.wScan = 0;
	input.ki.time = 0;
	input.ki.dwFlags = key_board.key_status;
	input.ki.dwExtraInfo = 0;

	int ret = SendInput(1, &input, sizeof(INPUT));
	if (ret > 0) {
		printf("成功执行键盘事件：%d\r\n", key_board.virtual_code);
	}
	return 0;
}

int HandleTestConnect(Packet* packet) { return 0; }

int GetPacketLen(Packet* pck) {
	if (pck != NULL) return pck->header.body_len + sizeof(PacketHeader);
	return 0;
}

// ✅ 修复：重写 ParsePacket，增加严格的边界检查
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

int InitServer() {
	WSADATA wsadata;
	WSAStartup(MAKEWORD(2, 2), &wsadata);
	g_server_socket = socket(AF_INET, SOCK_STREAM, 0);
	if (g_server_socket == INVALID_SOCKET) return -1;

	SOCKADDR_IN server_addr;
	server_addr.sin_family = AF_INET;
	server_addr.sin_port = htons(9999);
	server_addr.sin_addr.S_un.S_addr = inet_addr("127.0.0.1");

	if (bind(g_server_socket, (sockaddr*)&server_addr, sizeof(SOCKADDR_IN)) == SOCKET_ERROR) return -2;
	if (listen(g_server_socket, 1) == SOCKET_ERROR) return -3;
	return 0;
}