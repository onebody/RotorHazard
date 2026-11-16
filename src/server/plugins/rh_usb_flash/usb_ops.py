'''RH-ENE 增量4：USB 编排层——枚举入口、构建→烧录→复验、PIO 可用性。

handler 薄调用本模块；测试经 probe/runner/ports 注入，不真开串口不真构建。
烧后复验判据=INFO 有答且 uptime 归零级（主 spec §2 烧后复验）。
'''

import os
import time

from .firmware_flash import flash_firmware
from .usb_devices import classify_device, parse_info_line
from .usb_probe import ProbeError, probe_device


FRESH_UPTIME_MS = 30000


def pio_available(pio_path):
    '''VM 降级判据：PIO 可执行文件不在 ⇒ 刷写禁用并给原因，不猜路径。'''
    return bool(pio_path) and os.path.isfile(pio_path)


def probe_one(port_path, probe=None):
    '''单口探测分类（usb_probe_dev handler 用）；失败记 unknown 不抛。'''
    if probe is None:
        probe = probe_device
    item = {'port': port_path, 'description': '',
            'kind': 'unknown', 'flash_default': None,
            'provisionable': False, 'net_status': None,
            'mac': None, 'uptime_ms': None, 'version': None,
            'info_raw': None, 'error': None}
    try:
        texts = probe(port_path)
        item.update(classify_device(parse_info_line(texts.get('info_text')),
                                    texts.get('net_text'),
                                    texts.get('eth_text')))
    except Exception as exc:
        item['error'] = str(exc)
    return item


def enumerate_devices(ports=None, probe=None):
    '''串口枚举 + 逐口探测分类。probe 失败的口记 unknown 不炸整个列表。

    ports 为 None 时真枚举（serial.tools.list_ports.comports()）；测试注入
    SimpleNamespace(device=..., description=...) 列表。
    '''
    if probe is None:
        probe = probe_device
    if ports is None:
        from serial.tools import list_ports
        ports = list_ports.comports()
    devices = []
    for entry in ports:
        item = probe_one(entry.device, probe=probe)
        item['description'] = getattr(entry, 'description', '') or ''
        devices.append(item)
    return devices


def cached_ip_for(cache_dir, env='esp32c5-eth'):
    '''从构建缓存 manifest 读最近一次注入的配网 IP；没有则 None。

    固件无运行时 ETHIP 查询命令，eth 设备「当前构建 IP」只能从产物记录读出。
    '''
    import glob
    import json
    best = None
    best_mtime = -1
    for manifest_path in glob.glob(os.path.join(cache_dir or '', '*.json')):
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            continue
        if manifest.get('env') != env or not manifest.get('injected_ip'):
            continue
        mtime = os.path.getmtime(manifest_path)
        if mtime > best_mtime:
            best_mtime = mtime
            best = manifest['injected_ip']
    return best


def fresh_boot(info):
    '''烧后复验判据：INFO 解析成功且 uptime 在新鲜阈值内。'''
    return bool(info) and info.get('uptime_ms', FRESH_UPTIME_MS + 1) < FRESH_UPTIME_MS


def flash_and_verify(port_path, env, project, cache_dir, runner,
                     injected_ip=None, probe=None, boot_s=2.0,
                     fresh_ms=FRESH_UPTIME_MS):
    '''构建（缓存命中跳过）→ 烧录 → 等 boot → no-reset 探 INFO → 复验。

    返回 {'ok', 'fresh', 'error', 'build'}；fresh=INFO 有答且 uptime < fresh_ms。
    probe 注入（默认 usb_probe.probe_device）；boot_s 为烧后等 boot 的秒数。
    '''
    if probe is None:
        probe = probe_device
    build = flash_firmware(env, port_path, project, cache_dir, runner,
                           injected_ip=injected_ip)
    time.sleep(boot_s)
    try:
        texts = probe(port_path)
    except Exception as exc:
        return {'ok': False, 'fresh': False, 'error': str(exc),
                'build': build}
    info = parse_info_line(texts.get('info_text'))
    if info is None:
        return {'ok': False, 'fresh': False,
                'error': 'verify: no INFO reply from {}'.format(port_path),
                'build': build}
    fresh = info['uptime_ms'] < fresh_ms
    return {'ok': fresh, 'fresh': fresh,
            'error': None if fresh else
            'verify: uptime {}ms not fresh'.format(info['uptime_ms']),
            'build': build}
