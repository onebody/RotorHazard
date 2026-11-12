'''Phase 2.4b：ethernet_nodes 包对外暴露的两组入口（N-4）——Interface 工厂与状态读取。

不导入 server.py：装配只依赖 Config + 注册表 + 接口，因此可以单独跑。
事件入口不在这里测——它就是 server.py 的 pass_record_callback（S-1/N-2），
由 test_ethernet_mount.py 在真装配上验收。

运行：cd src/tests && python -m unittest test_ethernet_assembly
'''

import os
import sys
import tempfile
import unittest

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)
sys.path.append(os.path.join(_SRC_DIR, 'server'))
sys.path.append(os.path.join(_SRC_DIR, 'interface'))

import ene_protocol as enp
from Config import Config
from ethernet_nodes import build_interface, node_status
from ethernet_nodes.node_registry import NodeRegistry

UID = 'LPTC5-001'
UID2 = 'LPTC5-002'
ADDR = ('192.0.2.10', 9022)
BOOT = 0x11223344


def announce(uid=UID, channels=1):
    return enp.Announcement(node_uid=uid, device_type='QiYun-LPTC5',
                            firmware_version='0.0-test', channel_count=channels,
                            caps=enp.V1_CAPS, rssi_unit=enp.RSSI_CODE)


def announce_frame(uid=UID, channels=1):
    return enp.Frame(msg_type=enp.MSG.NODE_ANNOUNCE,
                     payload=enp.encode_announce(announce(uid, channels)),
                     sequence=1, boot_id=BOOT, uptime_ms=1_000)


def register_frame(uid=UID, channels=1):
    payload = (enp.encode_announce(announce(uid, channels)) +
               b'\x02\x00\x00\x00\x00\x01' + b'\xc0\x00\x02\x0a' +
               enp.encode_config([(0, 9022)])[1:] + bytes([enp.BOOT_POWER_ON]))
    return enp.Frame(msg_type=enp.MSG.NODE_REGISTER, payload=payload,
                     sequence=2, boot_id=BOOT, uptime_ms=1_100)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='rh_ene_assembly_')
        self.config = Config(None, os.path.join(self.tmp, 'config.json'),
                             os.path.join(self.tmp, 'cfg_bkp'))
        self.registry = NodeRegistry(self.config)

    def enable(self):
        self.config.set_item('ETHERNET_NODES', 'ENABLED', True)

    def build(self):
        return build_interface(self.config)


class Factory(Harness):
    def test_a_disabled_subsystem_builds_no_interface(self):
        self.assertIsNone(self.build())

    def test_an_enabled_but_empty_registry_builds_a_listen_only_interface(self):
        '''D-2：发现要先能跑，接入才有的确认；此时一个座位都不给。'''
        self.enable()
        interface = self.build()
        self.assertIsNotNone(interface)
        self.assertEqual(0, interface.num_nodes)
        self.assertEqual([], interface.nodes)

    def test_the_factory_never_hands_the_interface_a_none_whitelist(self):
        '''D-14 的硬约束：装配层不得传 None（None 在接口层=全放行）。'''
        self.enable()
        self.assertIsNotNone(self.build().whitelist)
        self.assertEqual((), self.build().whitelist)

    def test_seats_and_whitelist_come_from_the_registry(self):
        self.enable()
        self.registry.confirm(UID, 2)
        self.registry.confirm(UID2, 3)
        interface = self.build()
        self.assertEqual(5, interface.num_nodes)
        self.assertEqual((UID, UID2), interface.whitelist)
        self.assertEqual({UID: 0, UID2: 2}, interface.seat_plan)

    def test_a_removed_node_keeps_its_seats_but_leaves_the_whitelist(self):
        self.enable()
        self.registry.confirm(UID, 2)
        self.registry.remove(UID)
        interface = self.build()
        self.assertEqual(2, interface.num_nodes)
        self.assertEqual((), interface.whitelist)
        self.assertEqual({UID: 0}, interface.seat_plan)

    def test_the_server_session_id_is_random_per_process_but_stable_across_reloads(self):
        '''SPEC §3.3：节点据它发现"服务器重启过"；重载接口不算重启服务器。'''
        self.enable()
        first, second = self.build(), self.build()
        self.assertNotEqual(0, first.server_session_id)
        self.assertEqual(first.server_session_id, second.server_session_id)

    def test_a_confirmed_node_is_admitted_at_its_planned_seat(self):
        self.enable()
        self.registry.confirm(UID, 2)
        interface = self.build()
        interface.handle_packet(register_frame(UID, 2).encode(), ADDR)
        session = interface.sessions[1]
        self.assertEqual(UID, session.node_uid)
        self.assertEqual(0, session.first_seat)

    def test_an_unconfirmed_node_is_refused_even_though_it_was_discovered(self):
        self.enable()
        interface = self.build()
        interface.handle_packet(announce_frame(UID, 2).encode(), ADDR)
        interface.handle_packet(register_frame(UID, 2).encode(), ADDR)
        self.assertIn(UID, interface.candidates)
        self.assertEqual({}, interface.sessions)
        self.assertEqual(1, interface.stats['unauth_drops'])


class Status(Harness):
    def test_status_of_a_disabled_subsystem_reports_disabled_without_an_interface(self):
        status = node_status(None, self.registry)
        self.assertFalse(status['enabled'])
        self.assertFalse(status['ready'])
        self.assertEqual([], status['candidates'])
        self.assertEqual([], status['nodes'])

    def test_status_lists_a_discovered_candidate_as_unconfirmed(self):
        self.enable()
        interface = self.build()
        interface.handle_packet(announce_frame(UID, 2).encode(), ADDR)
        status = node_status(interface, self.registry)
        self.assertEqual([{'uid': UID, 'device_type': 'QiYun-LPTC5',
                           'firmware_version': '0.0-test', 'channels': 2,
                           'confirmed': False}], status['candidates'])

    def test_status_marks_a_confirmed_candidate_and_reports_its_seat(self):
        self.enable()
        self.registry.confirm(UID, 2)
        interface = self.build()
        interface.handle_packet(announce_frame(UID, 2).encode(), ADDR)
        interface.handle_packet(register_frame(UID, 2).encode(), ADDR)
        status = node_status(interface, self.registry)
        self.assertTrue(status['candidates'][0]['confirmed'])
        self.assertEqual([{'uid': UID, 'first_seat': 0, 'channels': 2,
                           'admitted': True, 'online': True, 'offset_ms': None,
                           'clock_suspect': False}], status['nodes'])

    def test_status_reports_a_bind_failure_reason(self):
        self.enable()
        interface = self.build()
        interface.ready_failure_msg = 'ENP-W uplink UDP/9021: address in use'
        status = node_status(interface, self.registry)
        self.assertFalse(status['ready'])
        self.assertEqual('ENP-W uplink UDP/9021: address in use',
                         status['ready_failure_msg'])

    def test_status_carries_the_network_quality_counters(self):
        self.enable()
        interface = self.build()
        interface.handle_packet(b'RHEN', ADDR)
        self.assertEqual(1, node_status(interface, self.registry)['stats']['bad_length'])


if __name__ == '__main__':
    unittest.main()