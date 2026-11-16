'''RH-ENE 中继面板：三源合成 + 绑定四态 + tag 合并 + 事件面。

纯函数部分不碰 RH 核心（只 import 插件的子模块）；事件面走生产加载器。
运行：cd src/tests && RH_DATA_DIR=$(mktemp -d) python -m unittest test_ethernet_usb_relay
'''

import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)
sys.path.append(os.path.join(_SRC_DIR, 'server'))
sys.path.append(os.path.join(_SRC_DIR, 'server', 'util'))
sys.path.append(os.path.join(_SRC_DIR, 'server', 'plugins'))
sys.path.append(os.path.join(_SRC_DIR, 'interface'))
sys.path.append(_TESTS_DIR)

from plugins.rh_usb_flash import relay_view

GW = ('192.168.31.102', 8011)

NET_URL_GOOD = 'ws://192.168.31.102:8011/node'
DEV = {'port': '/dev/cu.usbmodemRELAY', 'description': 'USB JTAG',
       'kind': 'relay', 'version': '1.2.3', 'mac': '46:90:c4:02:b5:0c',
       'net_status': {'url': NET_URL_GOOD, 'on': True, 'sta': 3, 'ws': 1},
       'info_raw': 'INFO …', 'error': None}
NODES = {'ok': True, 'error': None, 'nodes': [
    {'id': 'widscan58-52a1d8', 'kind': 'relay', 'online': True,
     'bridge': 'ws://192.168.31.102:8011/serial?ep=node%3Awidscan58-52a1d8'}]}


class HostPortTests(unittest.TestCase):
    def test_bare_host_port_and_scheme_forms_are_the_same_key(self):
        """板上可能存裸 host:port，中继照样 online ⇒ 判据只能是 host:port。"""
        self.assertEqual(relay_view.hostport('192.168.31.102:8011'), GW)
        self.assertEqual(relay_view.hostport('ws://192.168.31.102:8011/node'), GW)
        self.assertEqual(relay_view.hostport('ws://192.168.31.102:8011/serial?ep=node%3Ax'), GW)

    def test_hostname_and_ip_are_not_mixed_up(self):
        self.assertNotEqual(relay_view.hostport('ws://gw.local:8011/node'), GW)

    def test_missing_port_defaults_to_80(self):
        self.assertEqual(relay_view.hostport('ws://gw.local/node'), ('gw.local', 80))

    def test_garbage_is_none_not_a_guess(self):
        self.assertIsNone(relay_view.hostport(''))
        self.assertIsNone(relay_view.hostport(None))
        self.assertIsNone(relay_view.hostport('ws://gw:notaport/node'))


class BindingStateTests(unittest.TestCase):
    def bind(self, **over):
        args = dict(mac='46:90:c4:02:b5:0c', net_status={'url': NET_URL_GOOD},
                    gw_host=GW[0], gw_port=GW[1],
                    tag_by_mac={}, local_ports=[])
        args.update(over)
        return relay_view.binding_state(**args)

    def test_url_pointing_elsewhere_is_offsite(self):
        self.assertEqual('offsite', self.bind(net_status={'url': 'ws://10.0.0.9:8011/node'}))

    def test_missing_gateway_config_is_offsite_not_a_guess(self):
        self.assertEqual('offsite', self.bind(gw_host='', gw_port=None))

    def test_no_tag_for_this_mac_is_unbound(self):
        self.assertEqual('unbound', self.bind())

    def test_tagged_but_port_absent_from_rh_is_missing(self):
        self.assertEqual('missing', self.bind(tag_by_mac={'46:90:c4:02:b5:0c': '/dev/ttys009'},
                                              local_ports=['/dev/ttys001']))

    def test_all_three_satisfied_is_ok(self):
        self.assertEqual('ok', self.bind(tag_by_mac={'46:90:c4:02:b5:0c': '/dev/ttys009'},
                                         local_ports=['/dev/ttys009']))

    def test_offsite_wins_over_the_other_states(self):
        """四态按顺序短路：指着别处时，即使已绑定也不许报 ok。"""
        self.assertEqual('offsite', self.bind(
            net_status={'url': 'ws://10.0.0.9:8011/node'},
            tag_by_mac={'46:90:c4:02:b5:0c': '/dev/ttys009'},
            local_ports=['/dev/ttys009']))


class TagMergeTests(unittest.TestCase):
    def test_merge_keeps_other_ports_untouched(self):
        """别人的标注（有线 USB 口）不许被抹掉——这是防覆盖的主力判据。"""
        original = {'/dev/ttyUSB0': {'label': 'wired A', 'transport': 'wired'}}
        merged = relay_view.merge_tag(original, '/dev/ttys009', 'relay-1',
                                      '46:90:c4:02:b5:0c')
        self.assertEqual(merged['/dev/ttyUSB0'], {'label': 'wired A',
                                                  'transport': 'wired'})
        self.assertEqual(merged['/dev/ttys009'],
                         {'label': 'relay-1', 'transport': 'wireless',
                          'relay_mac': '46:90:c4:02:b5:0c'})
        self.assertNotIn('/dev/ttys009', original, 'merge 不该就地修改入参')

    def test_merge_does_not_mutate_the_input_entry_either(self):
        original = {'/dev/ttys009': {'label': 'old', 'transport': 'wired'}}
        merged = relay_view.merge_tag(original, '/dev/ttys009', 'new', 'aa:bb')
        self.assertEqual(original['/dev/ttys009'], {'label': 'old', 'transport': 'wired'})
        self.assertEqual(merged['/dev/ttys009']['label'], 'new')

    def test_drop_removes_only_that_key(self):
        original = {'/dev/ttys009': {'label': 'x', 'transport': 'wireless',
                                     'relay_mac': 'aa:bb'},
                    '/dev/ttyUSB0': {'label': 'wired A', 'transport': 'wired'}}
        out = relay_view.drop_tag(original, '/dev/ttys009')
        self.assertEqual(list(out), ['/dev/ttyUSB0'])

    def test_drop_of_an_absent_key_is_a_no_op(self):
        self.assertEqual(relay_view.drop_tag({'a': {'label': 'x'}}, 'b'),
                         {'a': {'label': 'x'}})


class TagIndexTests(unittest.TestCase):
    def test_two_ports_claiming_one_mac_do_not_raise(self):
        """脏数据：同一 mac 挂在两个口上（手工改过 JSON / 绑错后没解绑）。
        不许抛，也不许把一行中继渲染两次；后写的那条赢。"""
        tags = {'/dev/ttys001': {'label': 'a', 'transport': 'wireless',
                                 'relay_mac': 'aa:bb'},
                '/dev/ttys002': {'label': 'b', 'transport': 'wireless',
                                 'relay_mac': 'aa:bb'}}
        self.assertEqual(relay_view.tag_index(tags), {'aa:bb': '/dev/ttys002'})

    def test_wired_tags_are_ignored_even_with_a_mac(self):
        tags = {'/dev/ttyUSB0': {'label': 'wired A', 'transport': 'wired',
                                 'relay_mac': 'aa:bb'}}
        self.assertEqual(relay_view.tag_index(tags), {})


class RowTests(unittest.TestCase):
    def rows(self, devices=(DEV,), tags=None, local_ports=(), gw=GW, payload=NODES):
        return relay_view.relay_rows(devices, payload, tags or {}, list(local_ports),
                                     gw[0], gw[1])

    def test_ok_row_carries_net_status_and_gateway_identity(self):
        out = self.rows(tags={'/dev/ttys009': {'label': 'relay-1',
                                               'transport': 'wireless',
                                               'relay_mac': DEV['mac']}},
                        local_ports=['/dev/ttys009'])
        row = out['rows'][0]
        self.assertEqual(row['binding'], 'ok')
        self.assertEqual(row['bound_port'], '/dev/ttys009')
        self.assertEqual(row['gateway']['id'], 'widscan58-52a1d8')
        self.assertTrue(row['gateway']['online'])
        self.assertEqual(row['net_status']['url'], NET_URL_GOOD)
        self.assertIsNone(row['command'])

    def test_known_gateway_that_is_offline_is_not_unknown(self):
        """bridge URL 对得上、但 online:false：known=True / online=False，两档分开。"""
        payload = {'ok': True, 'error': None, 'nodes': [
            {'id': 'widscan58-52a1d8', 'kind': 'relay', 'online': False,
             'bridge': 'ws://192.168.31.102:8011/serial?ep=node%3Awidscan58-52a1d8'}]}
        row = self.rows(payload=payload)['rows'][0]
        self.assertTrue(row['gateway']['known'])
        self.assertFalse(row['gateway']['online'])
        self.assertEqual(row['gateway']['id'], 'widscan58-52a1d8')

    def test_unmatched_gateway_is_unknown(self):
        """节点表里没有 host:port 对得上的条目 → known=False，id 为空。"""
        payload = {'ok': False, 'error': 'timed out', 'nodes': []}
        row = self.rows(payload=payload)['rows'][0]
        self.assertFalse(row['gateway']['known'])
        self.assertFalse(row['gateway']['online'])
        self.assertIsNone(row['gateway']['id'])

    def test_missing_row_hands_back_the_command_verbatim(self):
        out = self.rows(tags={'/dev/ttys009': {'label': 'relay-1',
                                               'transport': 'wireless',
                                               'relay_mac': DEV['mac']}},
                        local_ports=[])
        row = out['rows'][0]
        self.assertEqual(row['binding'], 'missing')
        self.assertIn('node_bridge.py --url', row['command'])
        self.assertIn('node%3Awidscan58-52a1d8', row['command'])

    def test_bare_s3_has_no_net_status_and_does_not_crash(self):
        bare = {'port': '/dev/cu.usbmodemBARE', 'kind': 's3', 'mac': 'aa:bb',
                'version': '1.0', 'net_status': None, 'info_raw': 'INFO …',
                'error': None}
        out = self.rows(devices=(bare,))
        self.assertEqual(out['rows'], [])
        self.assertEqual(out['others'][0]['kind'], 's3')

    def test_unknown_device_is_listed_separately_with_its_raw_text(self):
        unknown = {'port': '/dev/cu.nope', 'kind': 'unknown', 'mac': None,
                   'version': None, 'net_status': None,
                   'info_raw': 'ERR command', 'error': 'no INFO reply'}
        out = self.rows(devices=(unknown,))
        self.assertEqual(out['rows'], [])
        self.assertEqual(out['others'][0]['info_raw'], 'ERR command')

    def test_wired_devices_do_not_enter_the_relay_table(self):
        eth = dict(DEV, port='/dev/cu.usbmodemETH', kind='eth', net_status=None)
        out = self.rows(devices=(eth,))
        self.assertEqual(out['rows'], [])
        self.assertEqual(out['others'], [{'port': eth['port'], 'kind': 'eth',
                                          'info_raw': eth['info_raw'],
                                          'error': None}])


if __name__ == '__main__':
    unittest.main()
