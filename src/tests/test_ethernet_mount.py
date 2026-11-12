'''Phase 2.4c：ETHNODE 在真 server.py 装配里的挂载与 READY 态重载（C-3）。

用 test_server.py 同款夹具（SOCKET_IO test_client + rh_program_initialize），
因此走的是真的 _do_init_rh_interface / initialize_rh_interface / mapper，
不是替身。事件入口沿用 server.pass_record_callback（S-1/N-2），这里只验接线。

运行：cd src/tests && python -m unittest test_ethernet_mount
'''

import os
import sys
import tempfile
import unittest

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)
sys.path.append(os.path.join(_SRC_DIR, 'server'))
sys.path.append(os.path.join(_SRC_DIR, 'server', 'util'))
sys.path.append(os.path.join(_SRC_DIR, 'server', 'plugins'))
sys.path.append(os.path.join(_SRC_DIR, 'interface'))
sys.path.append(_TESTS_DIR)

os.environ['RH_NODES'] = '8'
os.environ['RH_DATA_DIR'] = tempfile.mkdtemp(prefix='rh_ene_mount_')

import server
from interface_mapper import InterfaceType
from RHRace import RaceStatus
from ethernet_nodes.node_registry import NodeRegistry

server.RaceContext.serverconfig.config['SECRETS']['ADMIN_USERNAME'] = ''
server.RaceContext.serverconfig.config['SECRETS']['ADMIN_PASSWORD'] = ''
server.RaceContext.serverconfig.config['GENERAL']['DEBUG'] = True

UID = 'LPTC5-001'
UID2 = 'LPTC5-002'


class EthernetMount(unittest.TestCase):
    def setUp(self):
        self.client = server.SOCKET_IO.test_client(server.APP)
        server.rh_program_initialize(reg_endpoints_flag=False)
        self.registry = NodeRegistry(server.RaceContext.serverconfig)

    def tearDown(self):
        server.stop_background_threads()
        self.client.disconnect()

    def enable(self):
        server.RaceContext.serverconfig.set_item('ETHERNET_NODES', 'ENABLED', True)

    def ethnode(self):
        for meta in server.RaceContext.interface.mapped_interfaces:
            if meta.type == InterfaceType.ETHNODE:
                return meta.interface
        return None

    def node_types(self):
        return [n.type for n in server.RaceContext.interface.node_map]

    #
    # 挂载
    #

    def test_a_disabled_subsystem_mounts_no_ethnode_interface(self):
        self.assertIsNone(self.ethnode())

    def test_an_enabled_but_empty_registry_mounts_a_listen_only_ethnode(self):
        '''D-2：发现要先能跑——即便一个座位都没确认，ETHNODE 接口也要在。'''
        self.enable()
        server.initialize_rh_interface()
        interface = self.ethnode()
        self.assertIsNotNone(interface)
        self.assertEqual(0, interface.num_nodes)

    def test_ethnode_is_appended_last_and_mock_indices_do_not_drift(self):
        self.enable()
        self.registry.confirm(UID, 2)
        server.initialize_rh_interface()
        mapper = server.RaceContext.interface
        self.assertEqual(InterfaceType.ETHNODE, mapper.mapped_interfaces[-1].type)
        types = self.node_types()
        self.assertEqual([InterfaceType.MOCK] * 8 + [InterfaceType.ETHNODE] * 2, types)
        self.assertEqual(list(range(10)),
                         [n.object.index for n in mapper.node_map])

    def test_the_ethnode_interface_is_wired_to_the_server_pass_record_callback(self):
        '''S-1/N-2：事件只走 server 的串行队列回调，不另开路径。'''
        self.enable()
        self.registry.confirm(UID, 2)
        server.initialize_rh_interface()
        self.assertIs(server.pass_record_callback, self.ethnode().pass_record_callback)

    def test_the_four_unconditional_mapper_forwards_do_not_raise(self):
        '''interface_mapper 对所有接口无条件转发的四个（:179/:225/:235/:241）。'''
        self.enable()
        self.registry.confirm(UID, 2)
        server.initialize_rh_interface()
        mapper = server.RaceContext.interface
        mapper.jump_to_bootloader()
        mapper.close_fwupd_serial_port()
        mapper.get_intf_total_error_count()
        mapper.set_intf_error_report_percent_limit(10)

    #
    # READY 态重载（C-3）
    #

    def test_reload_is_refused_while_racing(self):
        self.enable()
        self.registry.confirm(UID, 2)
        server.initialize_rh_interface()
        server.RaceContext.race.race_status = RaceStatus.RACING
        self.assertFalse(server.reload_ethernet_nodes())

    def test_reload_in_ready_state_rebuilds_seats_from_the_registry(self):
        self.enable()
        self.registry.confirm(UID, 2)
        server.initialize_rh_interface()
        server.RaceContext.race.race_status = RaceStatus.READY
        self.registry.confirm(UID2, 3)  # 操作员在 READY 态确认第二台
        self.assertTrue(server.reload_ethernet_nodes())
        interface = self.ethnode()
        self.assertEqual(5, interface.num_nodes)
        self.assertEqual({UID: 0, UID2: 2}, interface.seat_plan)
        self.assertEqual((UID, UID2), interface.whitelist)


if __name__ == '__main__':
    unittest.main()