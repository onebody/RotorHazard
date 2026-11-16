'''RH-ENE 中继面板的数据面：三源合成 + 绑定四态（纯函数，无 IO）。

源 1  USB 枚举结果（usb_ops.probe_one 的 dict）
源 2  网关 GET /api/nodes
源 3  配置 LOCAL_NODES.PORT_TAGS + mapper 里的 RH 串口口列表

中继的 USB 口（配置走它）与 RH 看到的 PTY 口（node_bridge 起的那个，数据走它）
不是同一个东西，local_nodes 那行里也没有中继身份 ⇒ 「这块中继有没有接上 RH」
推不出来，只能显式绑定。绑定键是 relay_mac，写进 PORT_TAGS 的值里；这是纯增项
——核心 local_nodes.py 只读 label / transport，多一个 key 照旧渲染。
'''

from urllib.parse import urlparse

SECTION = 'LOCAL_NODES'
TAG_KEY = 'PORT_TAGS'
WIRELESS = 'wireless'

# 绑定四态，按此顺序短路（Spec §3.2）。
OFFSITE, UNBOUND, MISSING, OK = 'offsite', 'unbound', 'missing', 'ok'


def hostport(url):
    '''把存下来的 url 归一成 (host, port)。板上可能是裸 host:port，中继照样拨得上，
    所以判据是 host:port，不是写法（relay_net.py:45 定死）。

    已知边界：/api/nodes 的 bridge host 是网关自己的网卡 IP，所以中继当初若用主机名
    拨过去，两边字符串不同 → 判 offsite。这是预期（面板会提示改回 IP），不许加 DNS
    解析去「修正」——那会把一个真实错配藏起来。'''
    text = (url or '').strip()
    if not text:
        return None
    parsed = urlparse(text if '//' in text else '//' + text)
    try:
        port = parsed.port
    except ValueError:
        return None
    return parsed.hostname, port or 80


def gateway_index(payload):
    '''/api/nodes 的 payload → {(host,port): node}；bridge 解析不出的条目跳过。'''
    out = {}
    for node in (payload or {}).get('nodes') or []:
        key = hostport(node.get('bridge'))
        if key is not None:
            out[key] = node
    return out


def tag_index(tags):
    '''PORT_TAGS → {relay_mac: rh 串口口}。只认 transport=wireless 且 relay_mac 非空。'''
    out = {}
    for port, tag in (tags or {}).items():
        tag = tag or {}
        mac = tag.get('relay_mac')
        if mac and tag.get('transport') == WIRELESS:
            out[mac] = port
    return out


def binding_state(mac, net_status, gw_host, gw_port, tag_by_mac, local_ports):
    '''四态：offsite → unbound → missing → ok。网关未配置也算 offsite（不猜）。'''
    if not gw_host or not gw_port:
        return OFFSITE
    if hostport((net_status or {}).get('url')) != (gw_host, gw_port):
        return OFFSITE
    port = (tag_by_mac or {}).get(mac)
    if port is None:
        return UNBOUND
    if port not in (local_ports or []):
        return MISSING
    return OK


def merge_tag(tags, port, label, mac):
    '''读回合并：只改自己那一个 key，别人的标注原样保留（绝不整表替换）。'''
    merged = {key: dict(value or {}) for key, value in (tags or {}).items()}
    entry = merged.get(port) or {}
    entry['label'] = label
    entry['transport'] = WIRELESS
    entry['relay_mac'] = mac
    merged[port] = entry
    return merged


def drop_tag(tags, port):
    '''同上：只删自己的 key。'''
    merged = {key: dict(value or {}) for key, value in (tags or {}).items()}
    merged.pop(port, None)
    return merged


def bridge_command(node):
    '''missing 态的补齐命令原文（插件绝不代跑进程）。'''
    url = (node or {}).get('bridge')
    if not url:
        return None
    return "python tools/real_laps_c/node_bridge.py --url '{}'".format(url)


def relay_rows(devices, gateway_payload, tags, local_ports, gw_host, gw_port):
    '''枚举结果 → {'rows': 中继表, 'others': 未进判定的设备}。

    只有 kind=relay 且有 net_status 的设备进表；裸 S3 / 未识别设备单列，
    摊出 info_raw（那是排查线，不是状态）。
    '''
    gw = gateway_index(gateway_payload)
    by_mac = tag_index(tags)
    rows, others = [], []
    for dev in devices or []:
        if dev.get('kind') != 'relay' or not dev.get('net_status'):
            others.append({'port': dev.get('port'), 'kind': dev.get('kind'),
                           'info_raw': dev.get('info_raw'),
                           'error': dev.get('error')})
            continue
        net = dev['net_status']
        node = gw.get(hostport(net.get('url')))
        state = binding_state(dev.get('mac'), net, gw_host, gw_port, by_mac,
                              local_ports)
        bound = by_mac.get(dev.get('mac'))
        rows.append({
            'port': dev.get('port'),
            'description': dev.get('description') or '',
            'mac': dev.get('mac'),
            'version': dev.get('version'),
            'net_status': net,
            'gateway': {'known': node is not None,
                        'online': bool((node or {}).get('online')),
                        'discovered': bool((node or {}).get('discovered')),
                        'id': (node or {}).get('id')},
            'bound_port': bound,
            'tag': (tags or {}).get(bound) if bound else None,
            'binding': state,
            'command': bridge_command(node) if state == MISSING else None,
        })
    return {'rows': rows, 'others': others}
