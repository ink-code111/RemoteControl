"""端到端冒烟测试：验证重构后的 rc_server 协议链路是否正常。

测试项：
  1) TestConnect(2026) 心跳回显  -> 验证 连接/收包/解帧/发包 全链路
  2) Screen(1) 请求            -> 验证 屏幕抓取 + PNG 编码 + 大帧传输
  3) 粘包验证：把两个帧拼成一次 send，服务端应能正确拆成两个包
"""
import socket
import struct
import sys

MAGIC = 0x55AA77CC
CMD_SCREEN = 1
CMD_TEST = 2026


def frame(cmd, body=b""):
    return struct.pack("<III", MAGIC, cmd, len(body)) + body


def recvn(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("connection closed, got %d/%d bytes" % (len(buf), n))
        buf += chunk
    return buf


def read_packet(sock):
    magic, cmd, ln = struct.unpack("<III", recvn(sock, 12))
    body = recvn(sock, ln)
    return magic, cmd, body


def main():
    ok = True
    s = socket.create_connection(("127.0.0.1", 9999), timeout=15)

    # ---- 1) 心跳回显 ----
    s.sendall(frame(CMD_TEST))
    magic, cmd, body = read_packet(s)
    if magic == MAGIC and cmd == CMD_TEST and body == b"":
        print("[PASS] 1) TestConnect 心跳回显正常")
    else:
        print("[FAIL] 1) 心跳异常: magic=%x cmd=%d body=%r" % (magic, cmd, body))
        ok = False

    # ---- 2) 屏幕帧 ----
    s.sendall(frame(CMD_SCREEN))
    magic, cmd, body = read_packet(s)
    png_sig = b"\x89PNG\r\n\x1a\n"
    if magic == MAGIC and cmd == CMD_SCREEN and body[:8] == png_sig:
        print("[PASS] 2) 屏幕帧正常: %d 字节, PNG 签名 %s" % (len(body), body[:8].hex()))
    else:
        print("[FAIL] 2) 屏幕帧异常: magic=%x cmd=%d len=%d head=%s"
              % (magic, cmd, len(body), body[:8].hex()))
        ok = False

    # ---- 3) 粘包：两个帧一次发出，服务端应拆成两个独立回包 ----
    s.sendall(frame(CMD_TEST) + frame(CMD_TEST))
    for i in range(2):
        magic, cmd, body = read_packet(s)
        if not (magic == MAGIC and cmd == CMD_TEST):
            print("[FAIL] 3) 粘包拆分第 %d 包异常: magic=%x cmd=%d" % (i + 1, magic, cmd))
            ok = False
            break
    else:
        print("[PASS] 3) 粘包拆分正常（2 帧合并发送 -> 2 个独立回包）")

    s.close()
    print("\n结果: " + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
