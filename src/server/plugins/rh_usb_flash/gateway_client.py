'''RH-ENE 中继面板的网关只读客户端。

判中继在不在只认 GET /api/nodes 与 /api/endpoints；WS attach /serial?ep= 有副作用，
会把整个 serve.mjs 打挂（既有实测），这里绝不这么做。

失败语义：超时 / 非 200 / 坏 JSON / 形状不对 / 没配网关 → ok=False + error，
节点列表为空。面板据此渲染「未知」，与 online:false 分两档——离线也不等于设备死了。
'''

import json
from urllib.request import urlopen

DEFAULT_TIMEOUT = 2.0


def gateway_base(host, port):
    '''配全了才给 base url；半配（缺 host 或缺 port）一律 None，不猜默认端口。'''
    if not host or not port:
        return None
    return 'http://{}:{}'.format(str(host).strip(), int(port))


def fetch_nodes(base_url, opener=None, timeout=DEFAULT_TIMEOUT):
    '''GET {base}/api/nodes → {'ok', 'nodes', 'error'}。任何失败都不抛。'''
    base = (base_url or '').rstrip('/')
    if not base:
        return {'ok': False, 'nodes': [], 'error': 'gateway not configured'}
    opener = opener if opener is not None else urlopen
    url = base + '/api/nodes'
    try:
        resp = opener(url, timeout=timeout)
        status = getattr(resp, 'status', 200)
        if status != 200:
            return {'ok': False, 'nodes': [], 'error': 'HTTP {}'.format(status)}
        payload = json.loads(resp.read().decode('utf-8'))
    except Exception as exc:
        return {'ok': False, 'nodes': [], 'error': str(exc)}
    nodes = payload.get('nodes') if isinstance(payload, dict) else None
    if not isinstance(nodes, list):
        return {'ok': False, 'nodes': [], 'error': 'unexpected payload'}
    return {'ok': True, 'nodes': nodes, 'error': None}
