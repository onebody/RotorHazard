'''RH-ENE 增量4：USB 串口 no-reset 探针（薄传输层）。

电平纪律照 tools/real_laps_c/c5_usb_probe.py：dtr=True rts=False（保持运行态，
C5 rev≤1.0 上拉 RTS 会打死板子）；open 后不做任何复位，串口独占由编排层保证。
serial_factory 注入（测试传假对象）；每条命令后等应答窗口，收满或超时为止。
'''

import time


class ProbeError(Exception):
    pass


def _drain(s, secs):
    deadline = time.monotonic() + secs
    out = b''
    while time.monotonic() < deadline:
        waiting = getattr(s, 'in_waiting', 0)
        if waiting:
            out += s.read(waiting)
        else:
            time.sleep(0.02)
    return out


def _transact(s, command, respond_s):
    s.reset_input_buffer()
    s.write((command + '\n').encode())
    s.flush()
    return _drain(s, respond_s).decode('utf-8', 'replace')


def probe_device(port_path, serial_factory=None, respond_s=1.5):
    '''对指定串口发 INFO / NET STATUS / ETHIP? 三连探测，返回原始应答文本。

    返回 {'info_text', 'net_text', 'eth_text'}（无应答为空串）。
    开口失败抛 ProbeError(port, reason)。波特率对 USB-CDC 只是形式
    （c5_usb_probe.py 实测注释），统一 921600。
    '''
    if serial_factory is None:
        import serial
        serial_factory = serial.Serial
    try:
        s = serial_factory(port_path, 921600, timeout=0.05)
    except Exception as exc:
        raise ProbeError('{}: {}'.format(port_path, exc))
    try:
        s.dtr, s.rts = True, False
        time.sleep(0.2)
        return {'info_text': _transact(s, 'INFO', respond_s),
                'net_text': _transact(s, 'NET STATUS', respond_s),
                'eth_text': _transact(s, 'ETHIP?', respond_s)}
    finally:
        try:
            s.close()
        except Exception:
            pass
