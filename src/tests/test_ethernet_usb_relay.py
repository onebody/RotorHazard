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

import tempfile as _tempfile

os.environ['RH_NODES'] = '8'
os.environ['RH_DATA_DIR'] = _tempfile.mkdtemp(prefix='rh_usb_relay_')

import server
from RHRace import RaceStatus
from plugins import rh_usb_flash
from plugins.rh_usb_flash import usb_ops

server.RaceContext.serverconfig.config['SECRETS']['ADMIN_USERNAME'] = ''
server.RaceContext.serverconfig.config['SECRETS']['ADMIN_PASSWORD'] = ''
server.RaceContext.serverconfig.config['GENERAL']['DEBUG'] = True


def setUpModule():
    plugin = server.plugin_class('rh_usb_flash', server.PROGRAM_DIR, False)
    assert server.load_plugin(plugin), 'plugin load failed: {}'.format(plugin.load_issue)


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


class GatewayClientTests(unittest.TestCase):
    def test_good_payload_returns_the_node_list(self):
        from plugins.rh_usb_flash import gateway_client

        class Resp:
            status = 200
            def read(self):
                return b'{"nodes":[{"id":"a","kind":"relay","online":true}],"servers":[]}'

        def opener(url, timeout=None):
            self.assertEqual(url, 'http://192.168.31.102:8011/api/nodes')
            return Resp()

        out = gateway_client.fetch_nodes('http://192.168.31.102:8011', opener=opener)
        self.assertTrue(out['ok'])
        self.assertEqual(out['nodes'][0]['id'], 'a')

    def test_timeout_becomes_unknown_not_an_exception(self):
        from plugins.rh_usb_flash import gateway_client

        def opener(url, timeout=None):
            raise TimeoutError('timed out')

        out = gateway_client.fetch_nodes('http://gw:8011', opener=opener)
        self.assertFalse(out['ok'])
        self.assertEqual(out['nodes'], [])
        self.assertIn('timed out', out['error'])

    def test_non_200_is_unknown(self):
        from plugins.rh_usb_flash import gateway_client

        class Resp:
            status = 502
            def read(self):
                return b''

        out = gateway_client.fetch_nodes('http://gw:8011', opener=lambda u, timeout=None: Resp())
        self.assertFalse(out['ok'])
        self.assertIn('502', out['error'])

    def test_bad_json_is_unknown(self):
        from plugins.rh_usb_flash import gateway_client

        class Resp:
            status = 200
            def read(self):
                return b'not json'

        out = gateway_client.fetch_nodes('http://gw:8011', opener=lambda u, timeout=None: Resp())
        self.assertFalse(out['ok'])

    def test_unexpected_shape_is_unknown(self):
        from plugins.rh_usb_flash import gateway_client

        class Resp:
            status = 200
            def read(self):
                return b'{"servers":[]}'

        out = gateway_client.fetch_nodes('http://gw:8011', opener=lambda u, timeout=None: Resp())
        self.assertFalse(out['ok'])
        self.assertEqual(out['nodes'], [])

    def test_no_gateway_configured_makes_no_request(self):
        from plugins.rh_usb_flash import gateway_client

        def opener(url, timeout=None):
            raise AssertionError('不该发请求')

        out = gateway_client.fetch_nodes(None, opener=opener)
        self.assertFalse(out['ok'])
        self.assertIn('not configured', out['error'])

    def test_gateway_base_refuses_half_a_config(self):
        from plugins.rh_usb_flash import gateway_client
        self.assertIsNone(gateway_client.gateway_base('', 8011))
        self.assertIsNone(gateway_client.gateway_base('10.0.0.5', None))
        self.assertEqual(gateway_client.gateway_base('10.0.0.5', 8011),
                         'http://10.0.0.5:8011')


TAG_REQ = {'usb_port': DEV['port'], 'node_port': '/dev/ttys009', 'label': 'relay-1'}
RETARGET_REQ = {'usb_port': DEV['port'], 'host': '192.168.31.102', 'port_num': 8011}


class RelayEvents(unittest.TestCase):
    def setUp(self):
        self.client = server.SOCKET_IO.test_client(server.APP)
        server.rh_program_initialize(reg_endpoints_flag=False)
        self.set_gateway('192.168.31.102', '8011')

    def tearDown(self):
        server.stop_background_threads()
        self.client.disconnect()

    def set_gateway(self, host, port):
        cfg = server.RaceContext.serverconfig
        cfg.set_item('USB_FLASH', 'GATEWAY_HOST', host)
        cfg.set_item('USB_FLASH', 'GATEWAY_PORT', port)

    def patch_enum(self, devices):
        orig = usb_ops.enumerate_devices
        usb_ops.enumerate_devices = lambda: list(devices)
        self.addCleanup(setattr, usb_ops, 'enumerate_devices', orig)

    def patch_probe(self, device=None):
        orig = usb_ops.probe_one
        usb_ops.probe_one = lambda port, **kw: dict(device or DEV)
        self.addCleanup(setattr, usb_ops, 'probe_one', orig)

    def patch_gateway(self, payload):
        from plugins.rh_usb_flash import gateway_client
        orig = gateway_client.fetch_nodes
        gateway_client.fetch_nodes = lambda base, **kw: dict(payload)
        self.addCleanup(setattr, gateway_client, 'fetch_nodes', orig)

    def patch_provision(self):
        '''记录 provision_relay 的调用；拒绝路径必须让它零调用（G7 的「零下发」）。'''
        from plugins.rh_usb_flash import provision
        calls = []
        orig = provision.provision_relay

        def fake(usb_port, host, port_num, **kw):
            calls.append((usb_port, host, port_num))
            return {'ok': True}

        provision.provision_relay = fake
        self.addCleanup(setattr, provision, 'provision_relay', orig)
        return calls

    def last_event(self, name):
        for ev in reversed(self.client.get_received()):
            if ev['name'] == name:
                return ev['args'][0]
        return None

    def set_race(self, status):
        orig = server.RaceContext.race.race_status
        server.RaceContext.race.race_status = status
        self.addCleanup(setattr, server.RaceContext.race, 'race_status', orig)

    def test_refresh_emits_a_row_per_relay(self):
        self.patch_enum([DEV])
        self.patch_gateway(NODES)
        self.client.emit('relay_refresh', {})
        payload = self.last_event('relay_table')
        self.assertIsNotNone(payload)
        self.assertEqual(len(payload['rows']), 1)
        self.assertEqual(payload['rows'][0]['binding'], 'unbound')

    def test_refresh_while_a_usb_job_runs_reports_busy(self):
        with rh_usb_flash.USB_LOCK:
            self.client.emit('relay_refresh', {})
            payload = self.last_event('relay_table')
        self.assertTrue(payload['busy'])

    def test_tag_merges_instead_of_replacing_the_whole_table(self):
        cfg = server.RaceContext.serverconfig
        cfg.set_item('LOCAL_NODES', 'PORT_TAGS',
                     {'/dev/ttyUSB0': {'label': 'wired A', 'transport': 'wired'}})
        self.patch_probe(DEV)
        self.client.emit('relay_tag', TAG_REQ)
        result = self.last_event('relay_result')
        self.assertTrue(result['ok'])
        tags = cfg.get_item('LOCAL_NODES', 'PORT_TAGS')
        self.assertEqual(tags['/dev/ttyUSB0'], {'label': 'wired A', 'transport': 'wired'})
        self.assertEqual(tags['/dev/ttys009']['relay_mac'], DEV['mac'])
        self.assertEqual(tags['/dev/ttys009']['transport'], 'wireless')

    def test_tag_succeeds_during_an_active_race(self):
        """只改显示的东西不该被 READY 门拦住（G7 的反向判据）。"""
        self.set_race(RaceStatus.RACING)
        self.patch_probe(DEV)
        self.client.emit('relay_tag', TAG_REQ)
        result = self.last_event('relay_result')
        self.assertTrue(result['ok'])
        self.assertNotIn('refused', result)

    def test_tag_refuses_a_port_that_is_not_a_relay(self):
        self.patch_probe({'port': DEV['port'], 'kind': 'c5', 'mac': None})
        self.client.emit('relay_tag', TAG_REQ)
        result = self.last_event('relay_result')
        self.assertFalse(result['ok'])
        self.assertIn('relay', result['error'])

    def test_unbind_removes_only_its_own_tag(self):
        cfg = server.RaceContext.serverconfig
        cfg.set_item('LOCAL_NODES', 'PORT_TAGS',
                     {'/dev/ttys009': {'label': 'x', 'transport': 'wireless',
                                       'relay_mac': DEV['mac']},
                      '/dev/ttyUSB0': {'label': 'wired A', 'transport': 'wired'}})
        self.client.emit('relay_unbind', {'node_port': '/dev/ttys009'})
        self.assertTrue(self.last_event('relay_result')['ok'])
        tags = cfg.get_item('LOCAL_NODES', 'PORT_TAGS')
        self.assertNotIn('/dev/ttys009', tags)
        self.assertIn('/dev/ttyUSB0', tags)

    def test_retarget_is_refused_during_a_race(self):
        calls = self.patch_provision()
        self.set_race(RaceStatus.RACING)
        self.client.emit('relay_retarget', RETARGET_REQ)
        payload = self.last_event('relay_retarget_result')
        self.assertFalse(payload['ok'])
        self.assertTrue(payload['refused'])
        self.assertEqual(calls, [], '比赛中不许往设备写任何字节')

    def test_retarget_refuses_without_a_configured_gateway(self):
        calls = self.patch_provision()
        self.set_gateway('', '')
        self.client.emit('relay_retarget', {'usb_port': DEV['port']})
        payload = self.last_event('relay_retarget_result')
        self.assertFalse(payload['ok'])
        self.assertIn('gateway', payload['error'])
        self.assertEqual(calls, [])

    def test_retarget_is_busy_while_a_usb_job_runs(self):
        calls = self.patch_provision()
        with rh_usb_flash.USB_LOCK:
            self.client.emit('relay_retarget', RETARGET_REQ)
            payload = self.last_event('relay_retarget_result')
        self.assertFalse(payload['ok'])
        self.assertEqual('busy', payload['error'])
        self.assertEqual(calls, [])


class RelayPage(unittest.TestCase):
    def test_page_renders_relay_section_and_port_options(self):
        response = server.APP.test_client().get('/usbflash')
        self.assertEqual(200, response.status_code)
        body = response.data.decode()
        for marker in ('relay-table', 'relay-rows', 'relay-others',
                       'relay-refresh', 'relay-port-select'):
            self.assertIn(marker, body)
        self.assertIn('relay_refresh', body)

    def test_merged_page_also_carries_the_ethernet_nodes_section(self):
        '''合并页：以太网节点区块与 USB 设备同页（原 settings 面板已移除）。'''
        response = server.APP.test_client().get('/usbflash')
        self.assertEqual(200, response.status_code)
        body = response.data.decode()
        for marker in ('eth-local-nodes', 'eth-candidates', 'eth-nodes',
                       'eth-stats', 'eth-reload', 'load_ethnodes'):
            self.assertIn(marker, body)


class InjectionGuards(unittest.TestCase):
    def setUp(self):
        self.client = server.SOCKET_IO.test_client(server.APP)
        server.rh_program_initialize(reg_endpoints_flag=False)

    def tearDown(self):
        server.stop_background_threads()
        self.client.disconnect()

    def test_settings_page_carries_no_plugin_panel(self):
        """用户裁定：设置页不再挂本插件面板，唯一入口是 /usbflash 合并页。
        panel markdown 那套注入面随之整体消失（页面脚本一律 .text() 注入）。"""
        self.assertFalse(hasattr(rh_usb_flash, 'REGISTERED_MARKDOWN'))
        names = [panel.name for panel in server.RHAPI.ui.panels]
        self.assertNotIn(rh_usb_flash.PANEL, names)

    def test_relay_events_require_auth(self):
        import AdminAuth
        from plugins.rh_usb_flash import gateway_client        # noqa: F401
        AdminAuth.set_admin_socket_auth_enabled(True)
        self.addCleanup(AdminAuth.set_admin_socket_auth_enabled, False)
        cfg = server.RaceContext.serverconfig
        cfg.set_item('SECRETS', 'ADMIN_USERNAME', 'admin')
        cfg.set_item('SECRETS', 'ADMIN_PASSWORD', 'pw')
        self.addCleanup(cfg.set_item, 'SECRETS', 'ADMIN_USERNAME', '')
        self.addCleanup(cfg.set_item, 'SECRETS', 'ADMIN_PASSWORD', '')
        orig_status = cfg.config_file_status
        cfg.config_file_status = 1
        self.addCleanup(setattr, cfg, 'config_file_status', orig_status)
        for event, payload in (('relay_refresh', {}),
                               ('relay_tag', TAG_REQ),
                               ('relay_unbind', {'node_port': '/dev/ttys009'}),
                               ('relay_retarget', RETARGET_REQ)):
            self.client.emit(event, payload)
        names = [ev['name'] for ev in self.client.get_received()]
        for name in ('relay_table', 'relay_result', 'relay_retarget_result'):
            self.assertNotIn(name, names)


if __name__ == '__main__':
    unittest.main()
