'''RH-ENE 增量4（插件 rh_usb_flash）：USB endpoints 接线门禁。

认证 / 赛中拒绝 / 串口任务互斥三条，加上 /usbflash 页面可渲染。事件与路由
都由插件 initialize() 注册，server.py 里没有 USB 代码。
用 test_server.py 同款夹具（SOCKET_IO test_client + rh_program_initialize）；
usb_list 的枚举入口 monkeypatch（测试不真开串口），真实构建/烧录/配网由
单测层覆盖，这里只验 guard 与互斥接线。
运行：cd src/tests && python -m unittest test_ethernet_usb_endpoints
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
os.environ['RH_DATA_DIR'] = tempfile.mkdtemp(prefix='rh_usb_ep_')

import server
from RHRace import RaceStatus
from plugins import rh_usb_flash
from plugins.rh_usb_flash import usb_ops

server.RaceContext.serverconfig.config['SECRETS']['ADMIN_USERNAME'] = ''
server.RaceContext.serverconfig.config['SECRETS']['ADMIN_PASSWORD'] = ''
server.RaceContext.serverconfig.config['GENERAL']['DEBUG'] = True


def setUpModule():
    '''走生产加载器（manifest + rhapi 版本门 + plugins.rh_usb_flash 导入路径）。'''
    plugin = server.plugin_class('rh_usb_flash', server.PROGRAM_DIR, False)
    assert server.load_plugin(plugin), 'plugin load failed: {}'.format(
        plugin.load_issue)

FLASH_REQ = {'port': '/dev/cu.fake', 'env': 'esp32c5-eth'}
PROVISION_REQ = {'port': '/dev/cu.fake', 'host': '192.168.31.102',
                 'port_num': 8011}


class UsbEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = server.SOCKET_IO.test_client(server.APP)
        server.rh_program_initialize(reg_endpoints_flag=False)

    def tearDown(self):
        server.stop_background_threads()
        self.client.disconnect()

    def patch_enum(self, devices):
        orig = usb_ops.enumerate_devices
        usb_ops.enumerate_devices = lambda: devices
        self.addCleanup(setattr, usb_ops, 'enumerate_devices', orig)

    def last_event(self, name):
        received = self.client.get_received()
        for ev in reversed(received):
            if ev['name'] == name:
                return ev['args'][0]
        return None

    def set_race(self, status):
        orig = server.RaceContext.race.race_status
        server.RaceContext.race.race_status = status
        self.addCleanup(setattr, server.RaceContext.race,
                        'race_status', orig)

    def test_usb_list_emits_enumerated_devices(self):
        devices = [{'port': '/dev/cu.fake', 'kind': 'relay',
                    'provisionable': True}]
        self.patch_enum(devices)
        self.client.emit('usb_list', {})
        payload = self.last_event('usb_devices')
        self.assertIsNotNone(payload)
        self.assertEqual(devices, payload['devices'])

    def test_usb_list_requires_auth(self):
        import AdminAuth
        AdminAuth.set_admin_socket_auth_enabled(True)
        self.addCleanup(AdminAuth.set_admin_socket_auth_enabled, False)
        server.RaceContext.serverconfig.set_item('SECRETS',
                                                 'ADMIN_USERNAME', 'admin')
        server.RaceContext.serverconfig.set_item('SECRETS',
                                                 'ADMIN_PASSWORD', 'pw')
        self.addCleanup(server.RaceContext.serverconfig.set_item,
                        'SECRETS', 'ADMIN_USERNAME', '')
        self.addCleanup(server.RaceContext.serverconfig.set_item,
                        'SECRETS', 'ADMIN_PASSWORD', '')
        # check_auth 对 config_file_status==0（无配置文件）直通——模拟已配置状态
        orig_status = server.RaceContext.serverconfig.config_file_status
        server.RaceContext.serverconfig.config_file_status = 1
        self.addCleanup(setattr, server.RaceContext.serverconfig,
                        'config_file_status', orig_status)
        self.client.emit('usb_list', {})
        received = [ev['name'] for ev in self.client.get_received()]
        self.assertNotIn('usb_devices', received)

    def test_usb_flash_refused_during_race(self):
        self.set_race(RaceStatus.RACING)
        self.client.emit('usb_flash', FLASH_REQ)
        payload = self.last_event('usb_flash_result')
        self.assertIsNotNone(payload)
        self.assertFalse(payload['ok'])
        self.assertTrue(payload['refused'])

    def test_usb_provision_refused_during_race(self):
        self.set_race(RaceStatus.RACING)
        self.client.emit('usb_provision', PROVISION_REQ)
        payload = self.last_event('usb_provision_result')
        self.assertIsNotNone(payload)
        self.assertFalse(payload['ok'])
        self.assertTrue(payload['refused'])

    def test_usb_flash_busy_while_job_in_progress(self):
        with rh_usb_flash.USB_LOCK:
            self.client.emit('usb_flash', FLASH_REQ)
            payload = self.last_event('usb_flash_result')
        self.assertIsNotNone(payload)
        self.assertFalse(payload['ok'])
        self.assertEqual('busy', payload['error'])

    def test_usb_test_reports_probe_reply(self):
        texts = {'info_text': 'INFO widscan58|esp32s3|2|1.2.3|abcd1234|wifi|'
                              '46:90:c4:02:b5:0c|1200\n',
                 'net_text': 'NET {"url":"ws://192.168.31.102:8011/node",'
                             '"on":true,"sta":3,"ws":1,'
                             '"ip4":"192.168.31.119","rssi":-52}\n',
                 'eth_text': ''}
        orig = usb_ops.probe_device
        usb_ops.probe_device = lambda port, **kw: texts
        self.addCleanup(setattr, usb_ops, 'probe_device', orig)
        self.client.emit('usb_test', {'port': '/dev/cu.fake'})
        payload = self.wait_event('usb_test_result')
        self.assertIsNotNone(payload)
        self.assertEqual('relay', payload['device']['kind'])
        self.assertEqual(1200, payload['device']['uptime_ms'])

    def test_the_plugin_page_route_renders_the_merged_panel(self):
        '''合并页：blueprint 路由 /usbflash 同时承载 USB 设备与以太网节点区块。'''
        response = server.APP.test_client().get('/usbflash')
        self.assertEqual(200, response.status_code)
        body = response.data.decode()
        for marker in ('usb-select', 'usb-do-flash', 'usb-do-provision',
                       'usb-do-test', 'usb_list', 'usb-check', 'usb-check-text',
                       'usb-env-note', 'usb-gw-host', 'usb-gw-port',
                       'usb-wired-ip', 'usb-settings', 'usb-set-pio',
                       'usb-set-project', 'usb-set-cache', 'usb-save-settings',
                       'eth-local-nodes', 'eth-candidates', 'eth-nodes',
                       'eth-reload', 'load_ethnodes'):
            self.assertIn(marker, body)
        self.assertIn('esp32s3-relay', body)
        self.assertIn('data-target="esp32c5"', body)

    def set_settings(self, **values):
        '''写 USB_FLASH 段并在清理时还原（模块级 config 会跨用例留存）。'''
        section = rh_usb_flash.SECTION
        cfg = server.RaceContext.serverconfig
        keys = ('PIO_PATH', 'PROJECT_DIR', 'CACHE_DIR',
                'GATEWAY_HOST', 'GATEWAY_PORT')
        orig = {key: cfg.get_item(section, key) for key in keys}

        def restore():
            for key, value in orig.items():
                cfg.set_item(section, key, value)
        self.addCleanup(restore)
        for key, value in values.items():
            cfg.set_item(section, key, value)

    def test_save_settings_writes_config(self):
        self.set_settings(PIO_PATH='', PROJECT_DIR='', CACHE_DIR='',
                          GATEWAY_HOST='', GATEWAY_PORT='')
        self.client.emit('usb_save_settings', {
            'pio_path': '/opt/pio', 'project': '/tmp/proj',
            'cache_dir': '/tmp/cache', 'gateway_host': '10.0.0.9',
            'gateway_port': '8012'})
        payload = self.last_event('usb_settings_result')
        self.assertTrue(payload['ok'])
        self.assertEqual({'pio_path': '/opt/pio', 'project': '/tmp/proj',
                          'cache_dir': '/tmp/cache', 'gateway': {
                              'host': '10.0.0.9', 'port': 8012}},
                         payload['settings'])

    def test_save_settings_rejects_non_numeric_port(self):
        self.set_settings(GATEWAY_PORT='')
        self.client.emit('usb_save_settings', {'gateway_port': 'abc'})
        payload = self.last_event('usb_settings_result')
        self.assertFalse(payload['ok'])
        self.assertEqual('', server.RaceContext.serverconfig.get_item(
            rh_usb_flash.SECTION, 'GATEWAY_PORT'))

    def test_provision_wired_flashes_with_injected_ip(self):
        '''有线配网：C5 设备 → 以指定 IP 构建注入并烧录，不走无线 NET。'''
        orig_probe = usb_ops.probe_one
        orig_flash = usb_ops.flash_and_verify
        orig_pio = usb_ops.pio_available
        calls = []

        def fake_flash(port, env, project, cache_dir, run,
                       injected_ip=None, boot_s=None):
            calls.append((port, env, injected_ip))
            return {'ok': True, 'fresh': True, 'port': port}

        usb_ops.probe_one = lambda port: {'port': port, 'kind': 'c5'}
        usb_ops.flash_and_verify = fake_flash
        usb_ops.pio_available = lambda path: True
        self.addCleanup(setattr, usb_ops, 'probe_one', orig_probe)
        self.addCleanup(setattr, usb_ops, 'flash_and_verify', orig_flash)
        self.addCleanup(setattr, usb_ops, 'pio_available', orig_pio)
        project = tempfile.mkdtemp(prefix='rh_fw_proj_')
        with open(os.path.join(project, 'platformio.ini'), 'w'):
            pass
        self.set_settings(PROJECT_DIR=project, CACHE_DIR=project)
        self.client.emit('usb_provision', {'port': '/dev/cu.fake',
                                           'wired_ip': '192.168.8.77'})
        payload = self.wait_event('usb_provision_result')
        self.assertTrue(payload['ok'])
        self.assertEqual('wired', payload['mode'])
        self.assertEqual('192.168.8.77', payload['injected_ip'])
        self.assertEqual([('/dev/cu.fake', 'esp32c5-eth', '192.168.8.77')],
                         calls)

    def test_second_initialize_does_not_re_register(self):
        '''reload/重装路径会再调一次 initialize；同名端点二次注册会抛错。'''
        rh_usb_flash.initialize(server.RHAPI)
        self.patch_enum([])
        self.client.emit('usb_list', {})
        self.assertIsNotNone(self.last_event('usb_devices'))

    def wait_event(self, name, timeout_s=2.0):
        '''usb job 是 gevent 后台任务：泵事件循环直到结果到达。'''
        import gevent
        deadline = gevent.get_hub().loop.now() + timeout_s
        while gevent.get_hub().loop.now() < deadline:
            payload = self.last_event(name)
            if payload is not None:
                return payload
            gevent.sleep(0.02)
        return self.last_event(name)


if __name__ == '__main__':
    unittest.main()
