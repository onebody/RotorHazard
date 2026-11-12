'''RH-ENE 增量2：本地串口原生计时节点的只读显示。

无线 C5 经 rftrack→node_bridge→PTY 走的是串口原生路径（InterfaceType.RH），
不在 ENP-W 的 ETHNODE 观测面里，所以 ethnodes 面板原本看不到它。本模块把
mapper 的 RH 串口接口里的节点按物理端口归组成设备，供面板显示「当前可用的
有线/无线节点」。纯只读：不参与选源/切换，也不触碰 ENP-W 的 candidates/nodes/stats。

传输标签来自配置（LOCAL_NODES.PORT_TAGS），配置优先于代码兜底——未标注的串口
不猜 USB/PTY，transport 留 None，前端渲染「未标注」。
'''

SECTION = 'LOCAL_NODES'


def _port_tags(config):
    if config is None:
        return {}
    return config.get_item(SECTION, 'PORT_TAGS') or {}


def local_node_status(mapper, config):
    '''枚举串口原生节点，按 .serial.port 归组成物理设备列表（首次出现顺序）。

    mapper 为 None / 无 RH 串口接口 / 无节点 → 返回 []。
    每个设备：{port, label, transport, seats, seat_indices, api_level,
              firmware_version, identified}。identified = 任一座位 api_level>0
    （RH 成功读到节点修订号），反映「已识别」而非实时心跳。
    '''
    if mapper is None:
        return []
    rh_interface = mapper.get_rh_interface()
    if rh_interface is None:
        return []

    tags = _port_tags(config)
    devices = {}
    order = []
    for node in rh_interface.nodes:
        serial = getattr(node, 'serial', None)
        port = getattr(serial, 'port', None)
        if not port:
            continue
        if port not in devices:
            tag = tags.get(port) or {}
            devices[port] = {
                'port': port,
                'label': tag.get('label') or port,
                'transport': tag.get('transport'),
                'seats': 0,
                'seat_indices': [],
                'api_level': 0,
                'firmware_version': None,
                'identified': False,
            }
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

    return [devices[port] for port in order]