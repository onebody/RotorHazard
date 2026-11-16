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
            reply = transact(command).strip()
            if not reply.startswith('OK'):
                return {'ok': False, 'online': False, 'error': reply or command}
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
