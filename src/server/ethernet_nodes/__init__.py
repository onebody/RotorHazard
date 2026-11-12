'''RH-ENE 服务器侧子系统（D-7=N-1：全部核心化，不做插件）。

对外只暴露 N-4 那三组入口：
  · Interface 工厂 —— build_interface()
  · 事件入口 —— 就是 server.py 的 pass_record_callback（S-1/N-2：只走串行队列），
    本包刻意不再包一层，免得出现第二条绕过队列的路径
  · 状态读取 —— node_status()
'''

import random

from EthernetInterface import EthernetInterface
from ethernet_nodes.node_registry import NodeRegistry
from ethernet_nodes.local_nodes import local_node_status

# SPEC §3.3：server_session_id 每个服务器进程随机一次，跨接口重载保持不变——
# 节点据它区分「服务器重启过」与「只是重载了接口」。故取模块级常量，不在工厂里现算。
SERVER_SESSION_ID = random.getrandbits(32) or 1


def build_interface(config, **kwargs):
    '''按注册表装配 ETHNODE 接口；子系统关闭时返回 None（server.py 据此不挂载）。

    D-14 硬约束：whitelist 永远来自注册表（可能是空元组=全拒绝），绝不交出 None。
    座位数、白名单、稳定 ID 预排（C-2）全部由注册表决定，工厂不自作主张。
    '''
    registry = NodeRegistry(config)
    if not registry.enabled:
        return None
    return EthernetInterface(num_nodes=registry.capacity,
                             whitelist=registry.whitelist,
                             seat_plan=registry.seat_plan,
                             server_session_id=SERVER_SESSION_ID,
                             **kwargs)


def node_status(interface, registry):
    '''N-4 状态读取：把接口观测面 + 注册表接入面拼成给 UI 的一份快照。

    interface 为 None（子系统关闭或尚未挂载）时只报注册表侧的 enabled，其余留空。
    '''
    if interface is None:
        return {'enabled': registry.enabled, 'ready': False, 'ready_failure_msg': None,
                'candidates': [], 'nodes': [], 'stats': {}}

    admitted = set(registry.whitelist)
    candidates = [{'uid': uid, 'device_type': announce.device_type,
                   'firmware_version': announce.firmware_version,
                   'channels': announce.channel_count, 'confirmed': uid in admitted}
                  for uid, announce in interface.candidates.items()]

    active = {session.node_uid: session for session in interface.sessions.values()
              if not session.superseded}
    nodes = []
    for entry in registry.entries:
        session = active.get(entry['uid'])
        nodes.append({'uid': entry['uid'], 'first_seat': entry['first_seat'],
                      'channels': entry['channels'], 'admitted': entry['admitted'],
                      'online': bool(session and not session.offline),
                      'offset_ms': session.offset_ms if session else None,
                      'clock_suspect': session.clock_suspect if session else False})

    return {'enabled': registry.enabled, 'ready': interface.ready,
            'ready_failure_msg': interface.ready_failure_msg,
            'candidates': candidates, 'nodes': nodes, 'stats': interface.stats}