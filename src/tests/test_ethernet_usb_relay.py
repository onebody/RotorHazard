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


if __name__ == '__main__':
    unittest.main()
