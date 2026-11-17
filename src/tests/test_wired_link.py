# -*- coding: utf-8 -*-
'''wired_link 纯函数测试：解析/四态/标注合并，无进程、无 IO。'''

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.realpath(__file__))))))

import importlib.util as _ilu

_SPEC = _ilu.spec_from_file_location('wired_link', os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    '..', 'server', 'bundled_plugins', 'rh_usb_flash', 'wired_link.py'))
wired_link = _ilu.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wired_link)


class TestParseHostPort(unittest.TestCase):
    def test_ui_input_wins(self):
        self.assertEqual(wired_link.parse_hostport(
            {'host': '10.0.0.5', 'port': '9999'}, '192.168.0.98', 12345),
            ('10.0.0.5', 9999))

    def test_fallback_to_config(self):
        self.assertEqual(wired_link.parse_hostport(
            {}, '192.168.0.98', '12345'), ('192.168.0.98', 12345))

    def test_default_port(self):
        self.assertEqual(wired_link.parse_hostport(
            {'host': 'c5'}, None, None), ('c5', 12345))

    def test_no_host_is_none(self):
        self.assertIsNone(wired_link.parse_hostport({}, '', ''))

    def test_bad_port_falls_back(self):
        self.assertEqual(wired_link.parse_hostport(
            {'host': 'c5', 'port': 'bogus'}, None, None), ('c5', 12345))
        self.assertIsNone(wired_link.parse_hostport(
            {'host': 'c5', 'port': '99999'}, None, None))


class TestState(unittest.TestCase):
    def test_offline_when_bridge_dead(self):
        self.assertEqual(wired_link.state(False, True, True, True),
                         wired_link.OFFLINE)

    def test_bridge_only_without_link(self):
        self.assertEqual(wired_link.state(True, False, True, False),
                         wired_link.BRIDGE_ONLY)

    def test_link_ready_vs_online(self):
        self.assertEqual(wired_link.state(True, True, True, False),
                         wired_link.LINK_READY)
        self.assertEqual(wired_link.state(True, True, True, True),
                         wired_link.ONLINE)


class TestTags(unittest.TestCase):
    def test_merge_touches_only_link(self):
        tags = {'/tmp/other': {'label': 'x', 'relay_mac': 'AA'}}
        merged = wired_link.merge_tag(tags, host='192.168.0.98')
        self.assertEqual(merged['/tmp/other']['relay_mac'], 'AA')
        self.assertEqual(merged[wired_link.LINK]['transport'], 'wired')
        self.assertEqual(merged[wired_link.LINK]['wired_host'], '192.168.0.98')

    def test_drop_removes_only_link(self):
        tags = {'/tmp/other': {'label': 'x'}, wired_link.LINK: {'label': 'y'}}
        merged = wired_link.drop_tag(tags)
        self.assertIn('/tmp/other', merged)
        self.assertNotIn(wired_link.LINK, merged)

    def test_merge_does_not_mutate_input(self):
        tags = {}
        wired_link.merge_tag(tags)
        self.assertEqual(tags, {})


class TestPidAlive(unittest.TestCase):
    def test_current_process_alive(self):
        self.assertTrue(wired_link.pid_alive(os.getpid()))

    def test_garbage_pid(self):
        self.assertFalse(wired_link.pid_alive('bogus'))
        self.assertFalse(wired_link.pid_alive(None))


class TestBridgeArgv(unittest.TestCase):
    def test_tcp_target_and_link(self):
        argv = wired_link.bridge_argv('/x/bridge.py', '192.168.0.98', 12345)
        self.assertEqual(argv, ['/x/bridge.py', '--to',
                                'tcp://192.168.0.98:12345',
                                '--link', wired_link.LINK])


if __name__ == '__main__':
    unittest.main()
