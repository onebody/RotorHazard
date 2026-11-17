"""统一本地代理：把任意上游（WS / TCP）桥成 RH 能打开的 PTY。

RH 的串口层只会 `serial.Serial(path)` 打开一个设备，不解析 `socket://` 之类 URL，
所以无线中继（rftrack 网关 WS）、有线节点（W5500 裸 TCP）和插在 USB 上的节点都必须
先变成一条 PTY。对桥来说三者都只是字节管道、不解析任何帧，唯一的差别是「怎么拨号」；
本程序把拨号方式参数化，并且支持按优先级挂多条上游做**无缝切换**：

    bridge.py --to serial:/dev/cu.usbmodem11101 \\
              --to ws://127.0.0.1:8011/serial?ep=node%3Awidscan58-52a1d8 \\
              --link /tmp/rh-node0
    bridge.py --to tcp://192.168.31.50:12345 --link /tmp/rh-w5500-1

`--to` 可重复，靠前的优先：当前一条断了就试下一条，重连时又从第一条开始试，
所以「插上网线用有线、拔了走无线」只是上游换了一根，RH 看到的仍是同一条 PTY、
同样的 8 个席位，不需要改配置或重启。

`--link` 把 PTY 从端钉成固定路径。openpty() 每次给的 /dev/ttysNNN 都不一样，而
RH 的 SERIAL_PORTS 是配置里的死字符串；不钉住就得每轮改配置重启 RH。桥自身重连
（网关踢线、节点主动 CLOSE、上游换源）既不换路径也不关主端，RH 那侧无感。

PTY 必须设成 raw：默认 canonical+echo 会篡改二进制字节，实测会把 0x22 探针吃掉。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import select
import socket
import sys
import threading
import time
import tty

RECONNECT_DELAY_MIN = 0.5
RECONNECT_DELAY_MAX = 5.0
STABLE_SESSION_SECONDS = 10.0
# RH 每轮轮询约 1.5s，连续这么久一个字节都没回来就当链路死了（拔网线时
# TCP 只挂在那里不报错，光靠 socket 自己的超时要等好几分钟）。
IDLE_TIMEOUT_SECONDS = 6.0
# 跑在兜底链路上时，隔这么久敲一次队首（有线），看主链是不是插回来了。
# 兜底链路自己不会断，没人敲就永远回不去有线。
PROBE_INTERVAL_SECONDS =5.0
PROBE_TIMEOUT_SECONDS = 1.5

WS_SCHEMES = ("ws", "wss")
TCP_SCHEMES = ("tcp", "")
SERIAL_SCHEMES = ("serial",)


class WsPtyBridge:
    """鸭子类型对端（receive(timeout)/send/close）与 PTY 主端 fd 之间的双向字节泵。"""

    def __init__(self, ws, master_fd, now=time.monotonic):
        self._ws = ws
        self._master = master_fd
        self._stop = threading.Event()
        self._dead = threading.Event()
        self._threads = []
        self._now = now
        self.last_rx = now()   # 上游最近一次给我们字节的时间
        self.last_tx = None    # RH 最近一次给上游发命令的时间（None=还没发过）
        self.error = None
        self.target_index = 0  # 这条会话接的是 targets 里的第几条（build_bridge 填）

    def stalled(self, now, idle_timeout=IDLE_TIMEOUT_SECONDS):
        """RH 在轮询、上游却已经静默太久：拔网线时 socket 不会主动报错。"""
        if self.last_tx is None:
            return False
        return now - self.last_tx < idle_timeout and now - self.last_rx > idle_timeout

    def start(self):
        for target in (self._ws_to_pty, self._pty_to_ws):
            t = threading.Thread(target=self._pump, args=(target,), daemon=True)
            t.start()
            self._threads.append(t)

    def _pump(self, target):
        try:
            target()
        except BaseException as exc:
            self.error = exc
        finally:
            self._dead.set()

    def wait(self, timeout=None):
        """任一泵线程退出即返回 True；超时返回 False。"""
        return self._dead.wait(timeout)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        try:
            self._ws.close()
        except Exception:
            # 对端已关时 simple_websocket 的 close() 会再抛 ConnectionClosed；
            # 清理失败不能把 supervise 打死（否则 PTY 主端被关、RH 写 EIO）。
            pass

    def _ws_to_pty(self):
        while not self._stop.is_set():
            data = self._ws.receive(timeout=0.2)
            if data:
                self.last_rx = self._now()
                os.write(self._master, data)

    def _pty_to_ws(self):
        while not self._stop.is_set():
            r, _, _ = select.select([self._master], [], [], 0.2)
            if r:
                data = os.read(self._master, 4096)
                if data:
                    self.last_tx = self._now()
                    self._ws.send(data)


def _probe_alive(connect, target, timeout=PROBE_TIMEOUT_SECONDS):
    """敲一下队首，只有真收到字节才算主链回来了。

    发 0x22（READ_REVISION_CODE）是只读幂等的，不会像重复读圈速那样吃掉节点
    FIFO；探测连接用完即弃，不接管成正式链路，避免应答混进 RH 的数据流。
    """
    peer = None
    try:
        peer = connect(target)
        peer.send(b"\x22")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if peer.receive(timeout=0.2):
                return True
        return False
    except Exception:
        # 连不上、发不出去、对端直接关：都算「还没回来」。
        return False
    finally:
        if peer is not None:
            with contextlib.suppress(Exception):
                peer.close()


def supervise(targets, bridge, master, connect, sleep=time.sleep, now=time.monotonic,
              stop=None, log=None, idle_timeout=IDLE_TIMEOUT_SECONDS,
              probe_interval=PROBE_INTERVAL_SECONDS, probe=None):
    """守着这条链路：泵线程一死就按优先级换上游，PTY 主端全程不关。

    targets 可以是一个 URL 或按优先级排好的列表。每次重连都从第一条开始试：
    有线插着就用有线，拔了自然落到后面的无线；换源对 RH 只是「同一条 PTY 上
    的字节停了 0.5 秒」，不换路径、不重启。

    从端路径必须跨重连稳定——RH 把它当串口一直开着，换路径等于要重启 RH。
    稳定过一阵的会话断开后退避降回最小值，让短暂闪断有机会在 RH 读超时内补上。

    跑在兜底链路上时，兜底自己不会断，所以每隔 probe_interval 主动敲一次队首
    （有线）：必须真收到字节才算主链回来了，否则「网线插了但节点没起来」会让桥
    反复空转。切回队首只是一次正常重连，RH 侧停顿 0.5 秒。
    """
    if isinstance(targets, str):
        targets = [targets]
    stop = stop if stop is not None else threading.Event()
    probe = probe or _probe_alive
    delay = RECONNECT_DELAY_MIN
    # 一起手可能就在兜底链路上（启动时有线就没插），探活要从真实所在的那条算起。
    idx = getattr(bridge, "target_index", 0) if bridge is not None else 0
    stalled = False
    try:
        while not stop.is_set():
            if bridge is not None:
                started = now()
                stalled = False
                home = False
                last_probe = now()
                while not stop.is_set() and not bridge.wait(0.2):
                    # 有线网线被拔时 TCP 不会立刻报错，靠这里判静默才能及时换源。
                    if bridge.stalled(now(), idle_timeout):
                        bridge.error = ConnectionError("上游静默超时")
                        stalled = True
                        break
                    # 兜底链路不会自己断，主链插回来也没人知道：低频敲一下队首。
                    if (probe_interval and len(targets) > 1 and idx % len(targets) != 0
                            and now() - last_probe >= probe_interval):
                        last_probe = now()
                        if probe(connect, targets[0]):
                            home = True
                            break
                bridge.stop()
                if stop.is_set():
                    return
                if home:
                    if log:
                        log(f"主链恢复，切回 {targets[0]}")
                    idx = 0
                    delay = RECONNECT_DELAY_MIN
                    bridge = None
                    continue
                if log:
                    log(f"链路断开：{bridge.error!r}")
                # 静默的那条不算能用：往前挪一条，否则「连得上但不说话」的主链
                # 会让重连永远停在它身上，兜底那条一次都轮不到。
                idx = idx + 1 if stalled else 0
                stalled = False
                if now() - started >= STABLE_SESSION_SECONDS:
                    delay = RECONNECT_DELAY_MIN
                bridge = None
            idx %= len(targets)
            target = targets[idx]
            if log:
                log(f"{delay:.1f}s 后重连 {target}")
            sleep(delay)
            try:
                peer = connect(target)
            except Exception as exc:
                if log:
                    log(f"重连失败：{exc!r}")
                delay = min(delay * 2, RECONNECT_DELAY_MAX)
                idx += 1
                continue
            delay = RECONNECT_DELAY_MIN
            # idx 必须停在「这条会话真正接的是哪一条」上：落在兜底却记成 0，
            # 探活就再也不会跑，主链插回来也回不去。
            bridge = WsPtyBridge(peer, master, now=now)
            bridge.target_index = idx
            bridge.start()
    finally:
        if bridge is not None:
            bridge.stop()


class TcpNodeLink:
    """把 socket 适配成 WsPtyBridge 需要的 receive(timeout)/send/close。"""

    def __init__(self, sock):
        self._sock = sock

    def receive(self, timeout=None):
        self._sock.settimeout(timeout)
        try:
            data = self._sock.recv(4096)
        except TimeoutError:
            return None
        if not data:
            # 对端 FIN 是确定信号而不是「这一轮没数据」：泵必须为此退出，
            # supervise 才有机会接上下一个连接（固件的自愈路径会主动 CLOSE）。
            raise ConnectionError(f"节点关闭了连接：{self._describe()}")
        return data

    def _describe(self):
        try:
            return self._sock.getpeername()
        except OSError:
            return "已关闭的 socket"

    def send(self, data):
        self._sock.sendall(data)

    def close(self):
        self._sock.close()


class SerialLink:
    """把 pyserial 口适配成 WsPtyBridge 需要的 receive(timeout)/send/close。

    读空是「这次没数据」而不是「对端关了」，返回 None 让泵继续转；真掉线时
    pyserial 抛 SerialException，泵线程退出、supervise 接着换下一条上游。
    """

    def __init__(self, ser):
        self._ser = ser

    def receive(self, timeout=None):
        self._ser.timeout = timeout
        return self._ser.read(4096) or None

    def send(self, data):
        self._ser.write(data)

    def close(self):
        with contextlib.suppress(Exception):
            self._ser.close()


def parse_tcp(spec):
    """`tcp://host:port` 或裸 `host:port` → (host, port)。"""
    rest = spec.rpartition("://")[2]
    host, _, port = rest.rpartition(":")
    return host, int(port)


def serial_path(spec):
    """`serial:/dev/cu.usbmodem11101` → 设备路径。"""
    return spec.split(":", 1)[1]


def connect_ws(url):
    import simple_websocket

    return simple_websocket.Client.connect(url)


def connect_tcp(target):
    host, port = parse_tcp(target)
    sock = socket.create_connection((host, port), timeout=5.0)
    _tune_keepalive(sock)
    return TcpNodeLink(sock)


def _tune_keepalive(sock):
    """把「网线被拔」在十秒内变成可读到的错误。

    有线节点是 W5500 直连：网线一拔，socket 本身不会立刻报错，默认要重传好几分钟
    才超时；这期间桥以为自己还连着，RH 只能读到空洞。开 keepalive 把空转压到
    ~10s，supervise 才有机会切到后面的无线上游。
    """
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPALIVE", 3), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 3)):
        opt = getattr(socket, name, None)
        if opt is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)


def connect_serial(target):
    """打开本地 USB 串口节点。

    必须像 RH 那样先 setDTR(0)/setRTS(0) 再 open：这两根线常被硬件拿去复位
    节点，默认拉高会在打开瞬间把 C5 重启一遍（重启期间读到的全是失败）。
    """
    import serial

    ser = serial.Serial(port=None, baudrate=115200, timeout=0.25, write_timeout=1.0)
    try:
        ser.setDTR(0)
        ser.setRTS(0)
        ser.setPort(serial_path(target))
        ser.open()
    except BaseException:
        with contextlib.suppress(Exception):
            ser.close()
        raise
    return SerialLink(ser)


def scheme_of(target):
    """取 URL scheme。

    不能用 urlparse 一刀切：`serial:/dev/cu.x` 是单斜杠形式，而裸 `host:port`
    会被 urlparse 把主机名当 scheme（`localhost:12345` → scheme='localhost'）。
    写了 `://` 就必须是我们认识的 scheme（拼错要立刻报错）；只有单斜杠或裸
    host:port 时才按白名单猜，猜不出就是 TCP。
    """
    head = target.split(":", 1)[0].lower()
    if "://" in target:
        return head
    known = WS_SCHEMES + TCP_SCHEMES + SERIAL_SCHEMES
    return head if ":" in target and head in known else ""


def connector_for(target):
    """按 scheme 选拨号函数；裸 host:port 沿用有线节点的 TCP 语义。"""
    scheme = scheme_of(target)
    if scheme in WS_SCHEMES:
        return connect_ws
    if scheme in TCP_SCHEMES:
        return connect_tcp
    if scheme in SERIAL_SCHEMES:
        return connect_serial
    raise ValueError(
        f"不支持的链路：{target!r}（只认 ws:// wss:// tcp:// serial:/dev/… 或 host:port）")


def connect_any(target):
    return connector_for(target)(target)


def build_bridge(targets, connect=None, link=None):
    """开一对 raw PTY、按优先级连上第一条能通的 upstream。

    返回 (bridge, master_fd, slave_fd)。全部连不上时抛 ConnectionError，
    消息里逐条列出失败原因（运维一眼能看出是「占线」还是「没插」）。
    真实运行按每条 target 的 scheme 选拨号函数；测试可注入统一的 connect。

    link 给定时把从端钉成一个稳定路径：openpty() 的从端名每次都不一样，
    RH 的 SERIAL_PORTS 要跨重启稳定就得靠它（等价于 socat 的 link= 参数）。
    """
    if isinstance(targets, str):
        targets = [targets]
    if connect is None:
        connect = connect_any
    master, slave = os.openpty()
    tty.setraw(slave)
    errors = []
    peer = None
    index = 0
    for index, target in enumerate(targets):
        try:
            peer = connect(target)
            break
        except Exception as exc:
            errors.append(f"{target}：{exc}")
    if peer is None:
        os.close(master)
        os.close(slave)
        raise ConnectionError('；'.join(errors) or '没有可用的上游')
    if link:
        with contextlib.suppress(OSError):
            os.unlink(link)
        os.symlink(os.ttyname(slave), link)
    bridge = WsPtyBridge(peer, master)
    bridge.target_index = index
    return bridge, master, slave


def main(argv=None, connect=None, stop=None):
    parser = argparse.ArgumentParser(description="节点本地代理：WS / TCP / 串口 → RH 可打开的 PTY")
    parser.add_argument("--to", required=True, action="append", metavar="URL",
                        help="ws://… / tcp://host:port / serial:/dev/…（裸 host:port 按 TCP）；"
                             "可重复，靠前的优先，断了自动换下一条")
    parser.add_argument("--link", metavar="PATH",
                        help="把 PTY 从端钉成这个稳定路径（写进 RH SERIAL_PORTS 用）")
    args = parser.parse_args(argv)

    if connect is None:
        for target in args.to:
            try:
                connector_for(target)
            except ValueError as e:
                print(e, file=sys.stderr, flush=True)
                return 1
        connect = connect_any

    try:
        bridge, master, slave = build_bridge(args.to, connect=connect, link=args.link)
    except Exception as e:
        print(f"连接失败：{e}", file=sys.stderr, flush=True)
        return 1

    print(args.link or os.ttyname(slave), flush=True)
    bridge.start()
    try:
        # 单链路节点重开连接时桥必须自己接上，PTY 主端全程不关：RH 把从端
        # 路径当串口一直开着，掉进「连接死了但桥还活着」会让它永久挂在空洞上。
        supervise(args.to, bridge, master, connect, stop=stop,
                  log=lambda msg: print(msg, file=sys.stderr, flush=True))
    except KeyboardInterrupt:
        pass
    finally:
        os.close(master)
        os.close(slave)
    return 0


if __name__ == "__main__":
    sys.exit(main())
