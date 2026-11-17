'''RH-ENE 增量2：本地串口原生计时节点的只读显示（local_node_status）。

无线 C5 经 rftrack→bridge.py→PTY 走串口原生路径（InterfaceType.RH），不在
ENP-W 的 ETHNODE 观测面里。本测试覆盖：按物理端口归组、配置标签（有线/无线）、
未标注默认、已识别判据、空/无 RH 接口、以及 LOCAL_NODES 节注册不被 clean_config 抹掉。

实现经 mapper.get_rh_interface() 取串口接口，故不导入 interface_mapper/server，
不需要 RH_DATA_DIR 隔离。
运行：cd src/tests && python -m unittest test_ethernet_local_nodes
'''

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)
sys.path.append(os.path.join(_SRC_DIR, 'server'))
sys.path.append(os.path.join(_SRC_DIR, 'interface'))

from Config import Config
from ethernet_nodes.local_nodes import local_node_status

PORT_A = '/dev/ttys001'
PORT_B = '/dev/ttyUSB0'


def serial_node(index, port, api_level=35, firmware='1.2.3'):
    serial = SimpleNamespace(port=port) if port is not None else None
    return SimpleNamespace(index=index, serial=serial, api_level=api_level,
                           firmware_version_str=firmware)


def mapper_of(nodes):
    '''假 mapper：get_rh_interface() 返回一个只暴露 .nodes 的串口接口。'''
    iface = SimpleNamespace(nodes=nodes)
    return SimpleNamespace(get_rh_interface=lambda: iface)


def mapper_without_rh():
    return SimpleNamespace(get_rh_interface=lambda: None)


class LocalNodeHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='rh_ene_local_')
        self.config = Config(None, os.path.join(self.tmp, 'config.json'),
                             os.path.join(self.tmp, 'cfg_bkp'))

    def tag(self, port, label=None, transport=None):
        tags = self.config.get_item('LOCAL_NODES', 'PORT_TAGS') or {}
        entry = {}
        if label is not None:
            entry['label'] = label
        if transport is not None:
            entry['transport'] = transport
        tags[port] = entry
        self.config.set_item('LOCAL_NODES', 'PORT_TAGS', tags)


class Enumeration(LocalNodeHarness):
    def test_no_mapper_returns_empty(self):
        self.assertEqual([], local_node_status(None, self.config))

    def test_mapper_without_rh_interface_returns_empty(self):
        self.assertEqual([], local_node_status(mapper_without_rh(), self.config))

    def test_seats_on_one_port_collapse_into_one_device(self):
        nodes = [serial_node(i, PORT_A) for i in range(8)]
        devices = local_node_status(mapper_of(nodes), self.config)
        self.assertEqual(1, len(devices))
        self.assertEqual(PORT_A, devices[0]['port'])
        self.assertEqual(8, devices[0]['seats'])
        self.assertEqual(list(range(8)), devices[0]['seat_indices'])

    def test_two_ports_make_two_devices_in_first_seen_order(self):
        nodes = [serial_node(0, PORT_B), serial_node(1, PORT_A),
                 serial_node(2, PORT_B)]
        devices = local_node_status(mapper_of(nodes), self.config)
        self.assertEqual([PORT_B, PORT_A], [d['port'] for d in devices])
        self.assertEqual([2, 1], [d['seats'] for d in devices])

    def test_node_without_serial_port_is_skipped(self):
        nodes = [serial_node(0, None), serial_node(1, PORT_A)]
        devices = local_node_status(mapper_of(nodes), self.config)
        self.assertEqual(1, len(devices))
        self.assertEqual([1], devices[0]['seat_indices'])


class Tagging(LocalNodeHarness):
    def test_config_tag_sets_label_and_transport(self):
        self.tag(PORT_A, label='无线 C5', transport='wireless')
        device = local_node_status(mapper_of([serial_node(0, PORT_A)]),
                                   self.config)[0]
        self.assertEqual('无线 C5', device['label'])
        self.assertEqual('wireless', device['transport'])

    def test_untagged_port_defaults_label_to_port_and_transport_none(self):
        device = local_node_status(mapper_of([serial_node(0, PORT_A)]),
                                   self.config)[0]
        self.assertEqual(PORT_A, device['label'])
        self.assertIsNone(device['transport'])

    def test_wired_tag_is_preserved(self):
        self.tag(PORT_B, label='有线 USB', transport='wired')
        device = local_node_status(mapper_of([serial_node(0, PORT_B)]),
                                   self.config)[0]
        self.assertEqual('wired', device['transport'])
        self.assertEqual('有线 USB', device['label'])


class Identification(LocalNodeHarness):
    def test_positive_api_level_marks_device_identified(self):
        nodes = [serial_node(0, PORT_A, api_level=35, firmware='9.9.9')]
        device = local_node_status(mapper_of(nodes), self.config)[0]
        self.assertTrue(device['identified'])
        self.assertEqual(35, device['api_level'])
        self.assertEqual('9.9.9', device['firmware_version'])

    def test_zero_api_level_marks_device_unidentified(self):
        nodes = [serial_node(0, PORT_A, api_level=0, firmware=None)]
        device = local_node_status(mapper_of(nodes), self.config)[0]
        self.assertFalse(device['identified'])
        self.assertEqual(0, device['api_level'])
        self.assertIsNone(device['firmware_version'])

    def test_device_api_level_is_max_across_seats(self):
        nodes = [serial_node(0, PORT_A, api_level=0, firmware=None),
                 serial_node(1, PORT_A, api_level=35, firmware='1.0.0')]
        device = local_node_status(mapper_of(nodes), self.config)[0]
        self.assertEqual(35, device['api_level'])
        self.assertTrue(device['identified'])
        self.assertEqual('1.0.0', device['firmware_version'])


class ConfiguredPorts(LocalNodeHarness):
    '''单一启用要能「停用后再启用」：停用的口不能从列表里消失。'''

    def test_no_mapper_still_lists_configured_ports(self):
        self.config.set_item('GENERAL', 'SERIAL_PORTS', [PORT_A])
        devices = local_node_status(None, self.config)
        self.assertEqual([PORT_A], [d['port'] for d in devices])

    def test_configured_port_without_a_node_is_enabled_but_not_present(self):
        self.config.set_item('GENERAL', 'SERIAL_PORTS', [PORT_A])
        device = local_node_status(mapper_of([]), self.config)[0]
        self.assertTrue(device['enabled'])
        self.assertFalse(device['present'])
        self.assertEqual(0, device['seats'])

    def test_remembered_port_stays_listed_but_disabled(self):
        self.config.set_item('LOCAL_NODES', 'PORTS', [PORT_B])
        device = local_node_status(mapper_of([]), self.config)[0]
        self.assertEqual(PORT_B, device['port'])
        self.assertFalse(device['enabled'])
        self.assertFalse(device['present'])

    def test_live_port_is_enabled_only_when_the_scan_list_has_it(self):
        self.config.set_item('GENERAL', 'SERIAL_PORTS', [PORT_A])
        device = local_node_status(mapper_of([serial_node(0, PORT_A)]),
                                   self.config)[0]
        self.assertTrue(device['enabled'])
        self.assertTrue(device['present'])


class ConfigRegistration(LocalNodeHarness):
    def test_local_nodes_section_is_registered_so_clean_config_keeps_it(self):
        self.assertIn('LOCAL_NODES', self.config.config_sections)
        self.tag(PORT_A, label='无线 C5', transport='wireless')
        self.config.save_config()
        self.assertIn('LOCAL_NODES', self.config.config)
        reopened = Config(None, self.config.config_file_name,
                          self.config.cfg_bkp_dir_name)
        tags = reopened.get_item('LOCAL_NODES', 'PORT_TAGS')
        self.assertEqual({'label': '无线 C5', 'transport': 'wireless'},
                         tags[PORT_A])


if __name__ == '__main__':
    unittest.main()