'''Phase 2.4d：以太网节点确认/移除/重载/状态的 Socket.IO 入口 + /ethnodes 旧深链。

用 test_server.py 同款夹具。只验事件接线、READY 门（C-3）与旧深链跳转；
node_status 的字段形状由 test_ethernet_assembly 覆盖，这里不重复。
以太网节点 UI 已与 USB 设备合并到 /usbflash，本文件的页面用例只验 301。
浏览器内的点击交互未实测（见审计报告 §8.11 的显式声明）。

运行：cd src/tests && python -m unittest test_ethernet_ui
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
os.environ['RH_DATA_DIR'] = tempfile.mkdtemp(prefix='rh_ene_ui_')

import server
from interface_mapper import InterfaceType
from RHRace import RaceStatus
from ethernet_nodes.node_registry import NodeRegistry

server.RaceContext.serverconfig.config['SECRETS']['ADMIN_USERNAME'] = ''
server.RaceContext.serverconfig.config['SECRETS']['ADMIN_PASSWORD'] = ''
server.RaceContext.serverconfig.config['GENERAL']['DEBUG'] = True

UID = 'LPTC5-001'
UID2 = 'LPTC5-002'


class EthernetUI(unittest.TestCase):
    def setUp(self):
        self.client = server.SOCKET_IO.test_client(server.APP)
        server.rh_program_initialize(reg_endpoints_flag=False)
        # 配置是模块级单例，跨测试持久：每个用例前复位子系统，避免上一个用例的
        # ENABLED/NODES 泄漏进来（与 SECRETS 同款直接赋值，不落盘）。
        server.RaceContext.serverconfig.config['ETHERNET_NODES']['ENABLED'] = False
        server.RaceContext.serverconfig.config['ETHERNET_NODES']['NODES'] = []
        # 单一启用会改写串口扫描清单与「记住的口」，同样要在用例间复位。
        server.RaceContext.serverconfig.config['GENERAL']['SERIAL_PORTS'] = []
        server.RaceContext.serverconfig.config['LOCAL_NODES']['PORTS'] = []
        self.registry = NodeRegistry(server.RaceContext.serverconfig)

    def tearDown(self):
        server.stop_background_threads()
        self.client.disconnect()

    def enable(self):
        server.RaceContext.serverconfig.set_item('ETHERNET_NODES', 'ENABLED', True)

    def ready(self):
        self.enable()
        # 4.6.0 起 initialize_rh_interface() 不再自带 clear_interfaces()；rh_program_initialize
        # 之后重挂必须先清理，否则 MOCK/ETHNODE 二次追加、ethnode() 命中过期接口。
        server.RaceContext.interface.clear_interfaces()
        server.initialize_rh_interface()
        server.RaceContext.race.race_status = RaceStatus.READY

    def ethnode(self):
        for meta in server.RaceContext.interface.mapped_interfaces:
            if meta.type == InterfaceType.ETHNODE:
                return meta.interface
        return None

    def find(self, received, event):
        for resp in reversed(received):
            if resp['name'] == event:
                return resp['args'][0]
        self.fail('No response of type {0}'.format(event))

    def get_response(self, event):
        # get_received() 会清空队列：一次捕获后再扫描，拒绝例要同时看两个事件。
        return self.find(self.client.get_received(), event)

    #
    # 旧深链（页面已并入 /usbflash）
    #

    def test_the_ethnodes_page_redirects_to_the_merged_page(self):
        response = server.APP.test_client().get('/ethnodes')
        self.assertEqual(301, response.status_code)
        self.assertTrue(response.headers['Location'].endswith('/usbflash'))

    #
    # 状态读取
    #

    def test_status_event_reports_a_disabled_subsystem(self):
        self.client.emit('load_ethnodes', {})
        status = self.get_response('ethnodes_status')
        self.assertFalse(status['enabled'])
        self.assertEqual([], status['nodes'])

    def test_status_event_includes_local_nodes_without_disturbing_enpw_payload(self):
        '''增量2：_ethnodes_status 纯新增 local_nodes 键，ENP-W 字段形状不变。'''
        self.client.emit('load_ethnodes', {})
        status = self.get_response('ethnodes_status')
        self.assertIsInstance(status['local_nodes'], list)
        for key in ('enabled', 'ready', 'candidates', 'nodes', 'stats'):
            self.assertIn(key, status)

    def test_status_reports_one_table_with_the_single_active_key(self):
        '''面板只画一张表：串口口 + 以太网节点同列，启用项由 active 标出。'''
        self.ready()
        server._set_active_node('serial:/dev/rh-test')
        self.client.emit('load_ethnodes', {})
        status = self.get_response('ethnodes_status')
        self.assertTrue(status['subsystem']['enabled'])
        self.assertIn(status['uplink']['state'], ('ready', 'failed'))
        self.assertEqual(['serial:/dev/rh-test'], status['active'])
        rows = {row['key']: row for row in status['nodes_all']}
        self.assertTrue(rows['serial:/dev/rh-test']['enabled'])
        self.assertFalse(rows['serial:/dev/rh-test']['present'])  # 配置里有，机器上没有

    #
    # 单一启用（串口口 / 以太网节点二选一）
    #

    def test_activating_an_ethernet_node_keeps_only_one_enabled(self):
        self.ready()
        self.assertTrue(server._set_active_node('eth:' + UID, 2))
        self.assertEqual((UID,), self.registry.whitelist)
        self.assertEqual([], server.RaceContext.serverconfig.get_item('GENERAL', 'SERIAL_PORTS'))
        self.client.emit('load_ethnodes', {})
        status = self.get_response('ethnodes_status')
        self.assertEqual(['eth:' + UID], status['active'])
        row = status['nodes_all'][0]
        self.assertEqual('eth', row['kind'])
        # 测试夹具先挂了 8 个 mock 座位，ETHNODE 的 1 号座位=绝对第 9 座。
        self.assertEqual(9, row['first_seat'])
        self.assertTrue(row['enabled'])

    def test_activating_a_serial_node_disables_ethernet_and_remembers_the_port(self):
        self.ready()
        server._set_active_node('eth:' + UID, 2)
        self.assertTrue(server._set_active_node('serial:/dev/rh-test'))
        self.assertEqual((), self.registry.whitelist)
        self.assertEqual(['/dev/rh-test'],
                         server.RaceContext.serverconfig.get_item('GENERAL', 'SERIAL_PORTS'))
        self.assertIn('/dev/rh-test',
                      server.RaceContext.serverconfig.get_item('LOCAL_NODES', 'PORTS'))

    def test_deactivating_everything_leaves_no_node_enabled(self):
        self.ready()
        server._set_active_node('eth:' + UID, 2)
        self.assertTrue(server._set_active_node(''))
        self.assertEqual((), self.registry.whitelist)
        self.assertEqual([], server.RaceContext.serverconfig.get_item('GENERAL', 'SERIAL_PORTS'))
        self.client.emit('load_ethnodes', {})
        status = self.get_response('ethnodes_status')
        self.assertEqual([], status['active'])

    def test_activate_while_racing_is_refused(self):
        self.ready()
        server.RaceContext.race.race_status = RaceStatus.RACING
        self.client.emit('ethnodes_activate', {'key': 'eth:' + UID, 'channels': 2})
        received = self.client.get_received()
        self.find(received, 'ethnodes_status')
        self.assertEqual((), self.registry.whitelist)
        self.assertIn('message', self.find(received, 'priority_message'))

    def test_subsystem_toggle_writes_the_switch(self):
        self.ready()
        self.client.emit('ethnodes_subsystem', {'enabled': False})
        status = self.get_response('ethnodes_status')
        self.assertFalse(status['subsystem']['enabled'])
        self.assertEqual('off', status['uplink']['state'])

    #
    # 确认接入（READY 门）
    #

    def test_confirm_while_ready_admits_the_node_and_rebuilds_seats(self):
        self.ready()
        self.client.emit('ethnodes_confirm', {'uid': UID, 'channels': 2})
        status = self.get_response('ethnodes_status')
        self.assertIn(UID, self.registry.whitelist)
        self.assertEqual(2, self.ethnode().num_nodes)
        self.assertEqual([{'uid': UID, 'first_seat': 0, 'channels': 2,
                           'admitted': True, 'online': False, 'offset_ms': None,
                           'clock_suspect': False}], status['nodes'])

    def test_confirm_while_racing_is_refused_and_writes_nothing(self):
        self.ready()
        server.RaceContext.race.race_status = RaceStatus.RACING
        self.client.emit('ethnodes_confirm', {'uid': UID, 'channels': 2})
        received = self.client.get_received()
        self.find(received, 'ethnodes_status')
        self.assertEqual((), self.registry.whitelist)
        self.assertEqual(0, self.ethnode().num_nodes)
        self.assertIn('message', self.find(received, 'priority_message'))

    #
    # 移除（C-4：留占位）
    #

    def test_remove_while_ready_keeps_the_seat_but_leaves_the_whitelist(self):
        self.ready()
        self.registry.confirm(UID, 2)
        server.reload_ethernet_nodes()
        self.client.emit('ethnodes_remove', {'uid': UID})
        status = self.get_response('ethnodes_status')
        self.assertEqual((), self.registry.whitelist)
        self.assertEqual(2, self.ethnode().num_nodes)  # 占位仍在
        self.assertFalse(status['nodes'][0]['admitted'])

    #
    # 显式重载（READY 门）
    #

    def test_reload_while_racing_is_refused(self):
        self.ready()
        server.RaceContext.race.race_status = RaceStatus.RACING
        self.client.emit('ethnodes_reload', {})
        received = self.client.get_received()
        self.find(received, 'ethnodes_status')
        self.assertEqual(0, self.ethnode().num_nodes)
        self.assertIn('message', self.find(received, 'priority_message'))


if __name__ == '__main__':
    unittest.main()