'''RH-ENE 增量4：S3 中继「直接配网」编排。

链路照主 spec §2.2：NET STATUS →（需要时）NET URL/NET SAVE → NET STATUS 回执
→ NET OFF/NET ON 重连 → 网关轮询 online。幂等判据与 tools/real_laps_c/
relay_net.py 一致——板上存的是不带 scheme 不带路径的 host:port，所以只比
host:port 不比写法。网关侧只查节点表，不发 WS attach（红线）。
'''

import json
import time
from urllib.parse import urlparse

MAX_GATEWAY_POLLS = 15


def relay_target_url(host, port):
    return 'ws://{}:{}/node'.format(host, port)


def _hostport(url):
    text = (url or '').strip()
    if not text:
        return None
    parsed = urlparse(text if '//' in text else '//' + text)
    try:
        port = parsed.port
    except ValueError:
        return None
    return parsed.hostname, port or 80


def parse_status(text):
    '''取最后一行 `NET {…}` 的 JSON；没有（ERR 或静音）返回 None。'''
    found = None
    for line in (text or '').replace('\r\n', '\n').split('\n'):
        line = line.strip()
        if not line.startswith('NET {'):
            continue
        body = line[line.index('{'):line.rindex('}') + 1] if '}' in line else None
        if not body:
            continue
        try:
            parsed = json.loads(body)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            found = parsed
    return found


def plan_relay_commands(status, host, port):
    '''要发的写命令；已指目标就一条不发。status None 由调用方拦（拒发写命令）。'''
    if status is None:
        return None
    if _hostport(status.get('url')) == (host, port):
        return []
    return ['NET URL ' + relay_target_url(host, port), 'NET SAVE']


def provision_relay(port_path, host, port, serial_factory=None,
                    respond_s=1.5, post_s=2.0, gateway_nodes=None):
    '''把指定 USB 串口上的中继配到 ws://host:port/node 并重连。

    返回 {'ok', 'online', 'error'}：ok=False 时 error 带设备原文或判据名；
    online 由 gateway_nodes(url) 轮询独立判定，轮不到不算 ok 失败。
    '''
    from .usb_probe import _drain

    if serial_factory is None:
        import serial
        serial_factory = serial.Serial
    try:
        s = serial_factory(port_path, 921600, timeout=0.05)
    except Exception as exc:
        return {'ok': False, 'online': False,
                'error': 'serial {} {}'.format(port_path, exc)}

    target = relay_target_url(host, port)

    def transact(command):
        s.reset_input_buffer()
        s.write((command + '\n').encode())
        s.flush()
        return _drain(s, respond_s).decode('utf-8', 'replace')

    try:
        s.dtr, s.rts = True, False
        time.sleep(0.2)

        status = parse_status(transact('NET STATUS'))
        commands = plan_relay_commands(status, host, port)
        if commands is None:
            return {'ok': False, 'online': False, 'error': 'net_status'}

        written = False
        for command in commands:
            reply = transact(command)
            error = _reply_error(reply)
            if error:
                return {'ok': False, 'online': False, 'error': error}
            if not _reply_ok(reply):
                return {'ok': False, 'online': False,
                        'error': reply.strip() or command}
            written = True

        if written:
            transact('NET STATUS')

        transact('NET OFF')
        transact('NET ON')

        online = False
        if gateway_nodes is not None:
            for _ in range(MAX_GATEWAY_POLLS):
                if gateway_nodes(target):
                    online = True
                    break
                time.sleep(post_s)
        return {'ok': True, 'online': online, 'error': None}
    finally:
        try:
            s.close()
        except Exception:
            pass


def _reply_error(text):
    '''固件串口同时刷 LED/PIPE 调试行，只把 ERR 开头的行当失败原因。'''
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('ERR'):
            return line
    return None


def _reply_ok(text):
    '''OK 可能被调试行包着（甚至和上一条命令的 ACK 连成 "OK OK"）。'''
    return any(line.strip().startswith('OK') for line in text.splitlines())


def reconnect_relay(port_path, serial_factory=None, respond_s=1.5,
                    post_s=2.0, wait_s=20.0, gateway_nodes=None,
                    ssid=None, password=None):
    '''重连 WiFi，可选换 SSID/密码：写工作副本 + NET SAVE → OFF → ON。

    凭据留空表示沿用板上已存的值（密码只有给了 SSID 才会改）。NET ON 之后
    读 NET STATUS 拿 ip4/sta 作为「WiFi 连上了」的判据，再（若配了网关）
    轮询 /api/nodes 判 online。返回值里不含密码。
    '''
    from .usb_probe import _drain

    if serial_factory is None:
        import serial
        serial_factory = serial.Serial
    try:
        s = serial_factory(port_path, 921600, timeout=0.05)
    except Exception as exc:
        return {'ok': False, 'online': False,
                'error': 'serial {} {}'.format(port_path, exc)}

    def transact(command):
        s.reset_input_buffer()
        s.write((command + '\n').encode())
        s.flush()
        return _drain(s, respond_s).decode('utf-8', 'replace')

    def send(command):
        reply = transact(command)
        error = _reply_error(reply)
        if error:
            return error
        if not _reply_ok(reply):
            return reply.strip() or command
        return None

    try:
        s.dtr, s.rts = True, False
        time.sleep(0.2)

        status = parse_status(transact('NET STATUS'))
        if status is None:
            return {'ok': False, 'online': False, 'error': 'net_status'}
        target = status.get('url')

        if ssid:
            # 先改工作副本（掉电不丢旧配置），NET SAVE 才落盘。
            commands = ['NET SSID {}'.format(ssid)]
            if password:
                commands.append('NET PASS {}'.format(password))
            commands.append('NET SAVE')
            for command in commands:
                error = send(command)
                if error:
                    return {'ok': False, 'online': False, 'url': target,
                            'ssid': status.get('ssid'), 'error': error}

        # OFF 只是「确保先断开」，本来就可能是已断开（ERR net_stop），不判错。
        transact('NET OFF')
        reply = transact('NET ON')
        error = _reply_error(reply)
        if error:
            return {'ok': False, 'online': False, 'url': target,
                    'ssid': status.get('ssid'), 'error': error}
        if not _reply_ok(reply):
            return {'ok': False, 'online': False, 'url': target,
                    'ssid': status.get('ssid'),
                    'error': reply.strip() or 'NET ON'}

        online = False
        wifi = False
        link = {}
        ip4 = None
        deadline = time.time() + wait_s
        while True:
            link = parse_status(transact('NET STATUS')) or link
            ip4 = link.get('ip4')
            if not ip4 or ip4 == 'none':
                ip4 = None
            # 固件把 sta 报成布尔或 3（已连上）；没 ip4 一律算没连上。
            wifi = bool(ip4) and link.get('sta') in (3, True)
            if gateway_nodes is not None and target:
                if gateway_nodes(target):
                    online = True
                    break
            elif wifi:
                break
            if time.time() >= deadline:
                break
            time.sleep(post_s)

        return {'ok': True, 'online': online, 'url': target,
                'ssid': link.get('ssid') or ssid or status.get('ssid'),
                'ip4': ip4, 'wifi': wifi, 'error': None}
    finally:
        try:
            s.close()
        except Exception:
            pass
