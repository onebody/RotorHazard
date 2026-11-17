'''RH-ENE 增量2：本地串口原生计时节点的只读显示。

无线 C5 经 rftrack→bridge.py→PTY 走的是串口原生路径（InterfaceType.RH），
不在 ENP-W 的 ETHNODE 观测面里，所以 ethnodes 面板原本看不到它。本模块把
mapper 的 RH 串口接口里的节点按物理端口归组成设备，供面板显示「当前可用的
有线/无线节点」。纯只读：不参与选源/切换，也不触碰 ENP-W 的 candidates/nodes/stats。

传输标签来自配置（LOCAL_NODES.PORT_TAGS），配置优先于代码兜底——未标注的串口
不猜 USB/PTY，transport 留 None，前端渲染「未标注」。

面板要能「停用一个节点后再启用」，所以设备列表不只来自当前 mapper：
  · GENERAL.SERIAL_PORTS  = RH 真正在扫的口（在列表里 = 已启用）
  · LOCAL_NODES.PORTS     = 面板记住过的口（停用后仍列出来，才点得回去）
三类来源取并集，`present` 表示 RH 现在是否真的挂着这个口。
'''

SECTION = 'LOCAL_NODES'
SERIAL_SECTION = 'GENERAL'
SERIAL_KEY = 'SERIAL_PORTS'
PORTS_KEY = 'PORTS'


def _ports(config, section, key):
    if config is None:
        return []
    return list(config.get_item(section, key) or [])


def _port_tags(config):
    if config is None:
        return {}
    return config.get_item(SECTION, 'PORT_TAGS') or {}


def _device(port, tag, enabled):
    return {
        'port': port,
        'label': tag.get('label') or port,
        'transport': tag.get('transport'),
        'seats': 0,
        'seat_indices': [],
        'api_level': 0,
        'firmware_version': None,
        'identified': False,
        'present': False,
        'enabled': enabled,
    }


def local_node_status(mapper, config):
    '''枚举串口原生节点，按 .serial.port 归组成物理设备列表。

    顺序：mapper 里活着的口（首次出现顺序）→ 配置里已启用但当前不在的口 →
    只被面板记住的口。每项：{port, label, transport, seats, seat_indices,
    api_level, firmware_version, identified, present, enabled}。
    identified = 任一座位 api_level>0（RH 成功读到节点修订号），反映「已识别」
    而非实时心跳；enabled = 口在 GENERAL.SERIAL_PORTS 里（RH 正在扫它）。
    '''
    rh_interface = mapper.get_rh_interface() if mapper is not None else None

    tags = _port_tags(config)
    serial_ports = _ports(config, SERIAL_SECTION, SERIAL_KEY)
    devices, order = {}, []

    for node in (getattr(rh_interface, 'nodes', None) or []):
        serial = getattr(node, 'serial', None)
        port = getattr(serial, 'port', None)
        if not port:
            continue
        if port not in devices:
            devices[port] = _device(port, tags.get(port) or {},
                                    port in serial_ports)
            devices[port]['present'] = True
            order.append(port)
        device = devices[port]
        device['seats'] += 1
        device['seat_indices'].append(getattr(node, 'index', -1))
        api_level = getattr(node, 'api_level', 0) or 0
        if api_level > device['api_level']:
            device['api_level'] = api_level
        firmware = getattr(node, 'firmware_version_str', None)
        if firmware and not device['firmware_version']:
            device['firmware_version'] = firmware
        if api_level > 0:
            device['identified'] = True

    for port in serial_ports + _ports(config, SECTION, PORTS_KEY):
        if port and port not in devices:
            devices[port] = _device(port, tags.get(port) or {},
                                    port in serial_ports)
            order.append(port)

    return [devices[port] for port in order]
