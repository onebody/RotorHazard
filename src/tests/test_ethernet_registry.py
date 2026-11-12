'''Phase 2.4a：以太网节点注册表（D-2 发现≠接入的持久化那一半 + D-14 默认拒绝）。

只依赖 Config 与 ene_protocol，不导入 server，因此不需要 RH_DATA_DIR 隔离。
运行：cd src/tests && python -m unittest test_ethernet_registry
'''

import os
import sys
import tempfile
import unittest

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)
sys.path.append(os.path.join(_SRC_DIR, 'server'))
sys.path.append(os.path.join(_SRC_DIR, 'interface'))

from Config import Config
from ethernet_nodes.node_registry import NodeRegistry
import ene_protocol as enp

UID = 'LPTC5-001'
UID2 = 'LPTC5-002'


class RegistryHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='rh_ene_registry_')
        self.config = Config(None, os.path.join(self.tmp, 'config.json'),
                             os.path.join(self.tmp, 'cfg_bkp'))
        self.registry = NodeRegistry(self.config)

    def confirm(self, uid=UID, channels=1):
        return self.registry.confirm(uid, channels)


class Admission(RegistryHarness):
    def test_an_empty_registry_whitelists_nobody_and_needs_no_seats(self):
        '''D-14：默认拒绝。白名单必须是空元组而不是 None（None 在接口层=全放行）。'''
        self.assertEqual((), self.registry.whitelist)
        self.assertEqual(0, self.registry.capacity)
        self.assertEqual({}, self.registry.seat_plan)

    def test_confirming_a_node_whitelists_it_and_gives_it_the_first_seat(self):
        self.assertTrue(self.confirm(UID, 2))
        self.assertEqual((UID,), self.registry.whitelist)
        self.assertEqual({UID: 0}, self.registry.seat_plan)
        self.assertEqual(2, self.registry.capacity)

    def test_a_second_confirmation_appends_after_the_first_block(self):
        '''C-1：只能追加，不得插入或压缩。'''
        self.confirm(UID, 2)
        self.assertTrue(self.confirm(UID2, 3))
        self.assertEqual({UID: 0, UID2: 2}, self.registry.seat_plan)
        self.assertEqual(5, self.registry.capacity)
        self.assertEqual((UID, UID2), self.registry.whitelist)

    def test_confirming_the_same_node_twice_changes_nothing(self):
        self.confirm(UID, 2)
        self.assertTrue(self.confirm(UID, 2))
        self.assertEqual({UID: 0}, self.registry.seat_plan)
        self.assertEqual(2, self.registry.capacity)

    def test_resizing_an_already_seated_node_is_refused(self):
        '''C-1：块大小变了就装不进原座位，明拒而不是挪走别人。'''
        self.confirm(UID, 2)
        self.assertFalse(self.confirm(UID, 3))
        self.assertEqual({UID: 0}, self.registry.seat_plan)
        self.assertEqual(2, self.registry.capacity)

    def test_a_channel_count_outside_the_protocol_range_is_refused(self):
        for channels in (0, -1, enp.MAX_CHANNELS + 1):
            self.assertFalse(self.confirm(UID, channels), channels)
        self.assertEqual((), self.registry.whitelist)

    def test_an_empty_uid_is_refused(self):
        self.assertFalse(self.confirm('', 1))
        self.assertEqual((), self.registry.whitelist)


class Removal(RegistryHarness):
    def test_removing_a_node_keeps_its_seat_as_a_placeholder(self):
        '''C-4：移除=保留占位并标记不接入，容量与座位表都不缩。'''
        self.confirm(UID, 2)
        self.assertTrue(self.registry.remove(UID))
        self.assertEqual((), self.registry.whitelist)
        self.assertEqual({UID: 0}, self.registry.seat_plan)
        self.assertEqual(2, self.registry.capacity)

    def test_a_returning_node_takes_back_its_old_seat(self):
        self.confirm(UID, 2)
        self.registry.remove(UID)
        self.assertTrue(self.confirm(UID, 2))
        self.assertEqual({UID: 0}, self.registry.seat_plan)
        self.assertEqual((UID,), self.registry.whitelist)

    def test_a_new_node_does_not_reuse_a_placeholder(self):
        '''C-1：占位不是空位，新节点只能追加在容量之后。'''
        self.confirm(UID, 2)
        self.registry.remove(UID)
        self.confirm(UID2, 1)
        self.assertEqual({UID: 0, UID2: 2}, self.registry.seat_plan)
        self.assertEqual(3, self.registry.capacity)
        self.assertEqual((UID2,), self.registry.whitelist)

    def test_removing_an_unknown_node_is_refused(self):
        self.assertFalse(self.registry.remove('ROGUE'))


class Persistence(RegistryHarness):
    def test_confirmations_survive_a_fresh_registry_over_the_same_config_file(self):
        self.confirm(UID, 2)
        self.confirm(UID2, 1)
        self.registry.remove(UID)
        reopened = NodeRegistry(Config(None, self.config.config_file_name,
                                       self.config.cfg_bkp_dir_name))
        self.assertEqual((UID2,), reopened.whitelist)
        self.assertEqual({UID: 0, UID2: 2}, reopened.seat_plan)
        self.assertEqual(3, reopened.capacity)

    def test_the_ethernet_section_is_registered_so_clean_config_keeps_it(self):
        '''Config.clean_config 会删掉未注册的节（Config.py:369-390）。'''
        self.assertIn('ETHERNET_NODES', self.config.config_sections)
        self.confirm(UID, 1)
        self.config.save_config()
        self.assertIn('ETHERNET_NODES', self.config.config)

    def test_the_interface_is_disabled_until_the_operator_turns_it_on(self):
        self.assertFalse(self.registry.enabled)
        self.config.set_item('ETHERNET_NODES', 'ENABLED', True)
        self.assertTrue(NodeRegistry(self.config).enabled)


if __name__ == '__main__':
    unittest.main()