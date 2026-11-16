'''RH-ENE 增量4：USB 设备识别层（主 spec §1.2 探测法的解析与分类）。

探测顺序（主 spec §1.2）：NET STATUS 有结构化应答 ⇒ 无线中继；否则 ETHIP?
有应答 ⇒ 有线节点；两者皆 ERR ⇒ 裸 S3/裸 C5；INFO 无应答或不可解析 ⇒ 未识别。
ETHIP? 在当前固件（增量4 时点）恒为 ERR command——该分支为计划 B 预留，落地后
自动生效，不视为死代码。
'''

import json


def parse_info_line(text):
    '''取最后一行 `INFO ` 开头的应答，按固件格式（commands.c:438-453）解析。

    格式：INFO widscan58|<target>|<proto>|<version>|<commit>|<phy>|<mac>|<uptime_ms>
    任一字段缺失或 uptime 非数字 ⇒ None（半包/坏帧不猜）。
    '''
    found = None
    for line in (text or '').replace('\r\n', '\n').split('\n'):
        line = line.strip()
        if line.startswith('INFO '):
            found = line
    if not found:
        return None
    parts = found.split('|')
    if len(parts) != 8:
        return None
    try:
        uptime = int(parts[7])
    except ValueError:
        return None
    return {'app': parts[0].split(' ', 1)[1], 'target': parts[1],
            'proto': int(parts[2]), 'version': parts[3], 'commit': parts[4],
            'phy': parts[5], 'mac': parts[6], 'uptime_ms': uptime}


def _parse_net_status(text):
    '''取最后一行 `NET {...}` 的 JSON（判据沿用 tools/real_laps_c/relay_net.py）。'''
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


_FLASH_DEFAULT = {'relay': 'esp32s3-relay', 's3': 'esp32s3-relay',
                  'eth': 'esp32c5-eth', 'c5': 'esp32c5-eth'}


def classify_device(info, net, eth):
    '''三类探测文本 → 设备分类 dict。

    info 为 parse_info_line 结果（None=未识别）；net/eth 为对应命令的原始
    应答文本（空=无应答）。ETHIP 成功判据=应答非空且不以 ERR 开头——当前
    固件对未知命令一律 ERR command，未来计划 B 的 ETHIP? 格式不绑死在这里。
    '''
    if info is None:
        return {'kind': 'unknown', 'flash_default': None, 'provisionable': False,
                'net_status': None, 'mac': None, 'uptime_ms': None,
                'version': None, 'info_raw': (net or eth or '').strip() or None}
    info_raw = 'INFO ' + '|'.join(str(info[k]) for k in
                                  ('app', 'target', 'proto', 'version',
                                   'commit', 'phy', 'mac', 'uptime_ms'))
    if info['target'] == 'esp32s3':
        status = _parse_net_status(net)
        if status is not None:
            kind = 'relay'
        else:
            kind = 's3'
        provisionable = kind == 'relay'
    else:
        eth_ok = bool(eth) and not eth.strip().startswith('ERR')
        kind = 'eth' if eth_ok else 'c5'
        provisionable = eth_ok
    return {'kind': kind, 'flash_default': _FLASH_DEFAULT[kind],
            'provisionable': provisionable, 'net_status': status
            if info['target'] == 'esp32s3' and kind == 'relay' else None,
            'mac': info['mac'], 'uptime_ms': info['uptime_ms'],
            'version': info['version'], 'info_raw': info_raw}
