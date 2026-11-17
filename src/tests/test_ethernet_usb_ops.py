'''RH-ENE 增量4：USB 编排层（usb_ops）——枚举入口、烧后复验、PIO 可用性。

enumerate_devices 是 usb_list handler 的探测入口（probe 可注入，测试不真开串口）；
flash_and_verify 串起 firmware_flash 的构建→烧录→INFO 复验（uptime 归零级），
假 runner/假 probe 断言编排顺序与失败分支；pio_available 是 VM 降级判据
（路径不存在 ⇒ 面板禁用刷写并显示原因）。
运行：cd src/tests && python -m unittest test_ethernet_usb_ops
'''

import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)
sys.path.append(os.path.join(_SRC_DIR, 'server'))
sys.path.append(os.path.join(_SRC_DIR, 'interface'))

from plugins.rh_usb_flash.usb_ops import (cached_ip_for, enumerate_devices,
                                    flash_and_verify, fresh_boot,
                                    is_serial_candidate, list_serial_ports,
                                    pio_available, probe_one)
from plugins.rh_usb_flash.usb_probe import ProbeError


def relay_texts(url='ws://192.168.31.102:8011/node'):
    return {'info_text': 'INFO widscan58|esp32s3|2|1.2.3|abcd1234|wifi|'
                         '46:90:c4:02:b5:0c|4000\n',
            'net_text': ('NET {{"url":"{}","on":true,"sta":3,"ws":1,'
                         '"ip4":"192.168.31.119","rssi":-52}}\n').format(url),
            'eth_text': 'ERR command\n'}


def c5_texts():
    return {'info_text': 'INFO widscan58|esp32c5|2|1.2.3|abcd1234|wifi|'
                         '46:90:c4:02:b5:0d|4000\n',
            'net_text': 'ERR command\n',
            'eth_text': 'ERR command\n'}


def eth_texts():
    return {'info_text': 'INFO widscan58|esp32c5|2|1.2.3|abcd1234|eth|'
                         '46:90:c4:02:b5:0e|4000\n',
            'net_text': 'ERR command\n',
            'eth_text': 'ETHIP 192.168.31.150 255.255.255.0\n'}


class FakeProbe:
    def __init__(self, mapping):
        self.mapping = mapping
        self.probed = []

    def __call__(self, port_path, serial_factory=None, respond_s=1.5):
        self.probed.append(port_path)
        texts = self.mapping.get(port_path)
        if texts is None:
            raise ProbeError('{}: no device'.format(port_path))
        return texts


def ports_of(*paths):
    return [SimpleNamespace(device=p, description='USB Serial ' + p)
            for p in paths]


class Enumeration(unittest.TestCase):
    def test_empty_ports_list_yields_empty(self):
        self.assertEqual([], enumerate_devices([], probe=FakeProbe({})))

    def test_relay_port_classified_with_description(self):
        probe = FakeProbe({'/dev/cu.a': relay_texts()})
        devices = enumerate_devices(ports_of('/dev/cu.a'), probe=probe)
        self.assertEqual(1, len(devices))
        self.assertEqual('/dev/cu.a', devices[0]['port'])
        self.assertEqual('relay', devices[0]['kind'])
        self.assertEqual('USB Serial /dev/cu.a', devices[0]['description'])
        self.assertTrue(devices[0]['provisionable'])

    def test_bare_c5_classified_via_ethip_err(self):
        probe = FakeProbe({'/dev/cu.b': c5_texts()})
        devices = enumerate_devices(ports_of('/dev/cu.b'), probe=probe)
        self.assertEqual('c5', devices[0]['kind'])
        self.assertEqual('esp32c5-eth', devices[0]['flash_default'])

    def test_ethip_answer_marks_eth_kind(self):
        probe = FakeProbe({'/dev/cu.c': eth_texts()})
        devices = enumerate_devices(ports_of('/dev/cu.c'), probe=probe)
        self.assertEqual('eth', devices[0]['kind'])

    def test_probe_failure_becomes_unknown_entry_not_crash(self):
        probe = FakeProbe({})  # everything raises
        devices = enumerate_devices(ports_of('/dev/cu.dead'), probe=probe)
        self.assertEqual(1, len(devices))
        self.assertEqual('unknown', devices[0]['kind'])
        self.assertFalse(devices[0]['provisionable'])
        self.assertIn('/dev/cu.dead', devices[0]['error'])

    def test_ports_are_probed_in_given_order(self):
        probe = FakeProbe({'/dev/cu.a': relay_texts(),
                           '/dev/cu.b': c5_texts()})
        devices = enumerate_devices(ports_of('/dev/cu.a', '/dev/cu.b'),
                                    probe=probe)
        self.assertEqual(['/dev/cu.a', '/dev/cu.b'], probe.probed)
        self.assertEqual(['relay', 'c5'], [d['kind'] for d in devices])


class PortFiltering(unittest.TestCase):
    '''真枚举降噪：伪串口（macOS debug-console/蓝牙等）不进入探测队列。'''

    def test_usb_serial_with_vid_is_candidate(self):
        entry = SimpleNamespace(device='/dev/cu.usbmodem21201',
                                hwid='USB VID:PID=303A:1001', vid=0x303A)
        self.assertTrue(is_serial_candidate(entry))

    def test_pseudo_port_without_vid_and_na_hwid_is_skipped(self):
        entry = SimpleNamespace(device='/dev/cu.debug-console',
                                hwid='n/a', vid=None)
        self.assertFalse(is_serial_candidate(entry))

    def test_onboard_uart_with_real_hwid_is_candidate(self):
        entry = SimpleNamespace(device='/dev/ttyS0', hwid='PNP0501', vid=None)
        self.assertTrue(is_serial_candidate(entry))

    def test_real_enumeration_skips_pseudo_ports(self):
        try:
            from serial.tools import list_ports
        except ImportError:
            self.skipTest('pyserial not installed')
        entries = [SimpleNamespace(device='/dev/cu.debug-console',
                                  hwid='n/a', vid=None),
                   SimpleNamespace(device='/dev/cu.Bluetooth-Incoming-Port',
                                   hwid='n/a', vid=None),
                   SimpleNamespace(device='/dev/cu.usbmodem21201',
                                   hwid='USB VID:PID=303A:1001', vid=0x303A)]
        orig = list_ports.comports
        list_ports.comports = lambda: list(entries)
        self.addCleanup(setattr, list_ports, 'comports', orig)
        self.assertEqual(['/dev/cu.usbmodem21201'],
                         [e.device for e in list_serial_ports()])

    def test_keeps_all_when_nothing_matches(self):
        try:
            from serial.tools import list_ports
        except ImportError:
            self.skipTest('pyserial not installed')
        entries = [SimpleNamespace(device='/dev/cu.debug-console',
                                   hwid='n/a', vid=None)]
        orig = list_ports.comports
        list_ports.comports = lambda: list(entries)
        self.addCleanup(setattr, list_ports, 'comports', orig)
        self.assertEqual(['/dev/cu.debug-console'],
                         [e.device for e in list_serial_ports()])


class ProbeOne(unittest.TestCase):
    def test_classified_device_carries_port(self):
        probe = FakeProbe({'/dev/cu.a': relay_texts()})
        device = probe_one('/dev/cu.a', probe=probe)
        self.assertEqual('/dev/cu.a', device['port'])
        self.assertEqual('relay', device['kind'])
        self.assertIsNone(device['error'])

    def test_probe_failure_yields_unknown_with_error(self):
        probe = FakeProbe({})
        device = probe_one('/dev/cu.dead', probe=probe)
        self.assertEqual('unknown', device['kind'])
        self.assertFalse(device['provisionable'])
        self.assertIn('/dev/cu.dead', device['error'])


class CachedIp(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix='rh_usb_cacheip_')

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def manifest(self, name, ip, mtime=None):
        import json
        path = os.path.join(self.cache, name)
        with open(path + '.bin', 'wb') as f:
            f.write(b'fake')
        with open(path + '.json', 'w') as f:
            json.dump({'env': 'esp32c5-eth', 'injected_ip': ip,
                       'bin': path + '.bin'}, f)
        if mtime:
            os.utime(path + '.json', (mtime, mtime))

    def test_latest_eth_manifest_reports_injected_ip(self):
        self.manifest('usb_flash_esp32c5-eth_old.json', '192.168.0.111',
                      mtime=1000)
        self.manifest('usb_flash_esp32c5-eth_new.json', '192.168.0.222',
                      mtime=2000)
        self.assertEqual('192.168.0.222',
                         cached_ip_for(self.cache))

    def test_missing_cache_returns_none(self):
        self.assertIsNone(cached_ip_for('/nonexistent/cache/dir'))

    def test_non_eth_manifests_ignored(self):
        import json
        path = os.path.join(self.cache, 'usb_flash_esp32s3-relay.json')
        with open(path + '.bin', 'wb') as f:
            f.write(b'fake')
        with open(path + '.json', 'w') as f:
            json.dump({'env': 'esp32s3-relay', 'injected_ip': None,
                       'bin': path + '.bin'}, f)
        self.assertIsNone(cached_ip_for(self.cache))


class FreshBoot(unittest.TestCase):
    def test_small_uptime_is_fresh(self):
        self.assertTrue(fresh_boot({'uptime_ms': 5000}))

    def test_old_uptime_is_not_fresh(self):
        self.assertFalse(fresh_boot({'uptime_ms': 60000}))

    def test_missing_info_is_not_fresh(self):
        self.assertFalse(fresh_boot(None))


class FlashVerify(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.project = tempfile.mkdtemp(prefix='rh_usb_ops_proj_')
        self.cache = tempfile.mkdtemp(prefix='rh_usb_ops_cache_')
        # build_firmware 的工程存在性防线：目录内须有 platformio.ini
        with open(os.path.join(self.project, 'platformio.ini'), 'w') as f:
            f.write('[env:esp32c5-eth]\nplatform = espressif32\n')

    def tearDown(self):
        shutil.rmtree(self.project, ignore_errors=True)
        shutil.rmtree(self.cache, ignore_errors=True)

    def runner(self):
        def fake_runner(cmd, **kwargs):
            # 模拟真实 PIO：构建成功时在 cwd 产出 firmware.bin（flash 命令不产）
            is_flash = '-t upload' in cmd
            self.calls.append((cmd, kwargs.get('cwd'),
                               'flash' if is_flash else 'build'))
            if not is_flash:
                bin_dir = os.path.join(kwargs['cwd'], '.pio', 'build',
                                       'esp32c5-eth')
                os.makedirs(bin_dir, exist_ok=True)
                with open(os.path.join(bin_dir, 'firmware.bin'), 'wb') as f:
                    f.write(b'fake')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        return fake_runner

    def verify_probe(self, texts):
        return lambda port_path, **kw: texts

    def test_order_is_build_upload_then_verify_probe(self):
        probe = self.verify_probe(relay_texts())
        result = flash_and_verify('/dev/cu.a', 'esp32c5-eth', self.project,
                                  self.cache, self.runner(),
                                  probe=probe, boot_s=0)
        self.assertTrue(result['ok'])
        kinds = [c[2] for c in self.calls]
        self.assertEqual(['build', 'flash'], kinds)
        # upload 带显式端口
        flash_cmd = self.calls[1][0]
        self.assertIn('--upload-port', flash_cmd)
        self.assertIn('/dev/cu.a', flash_cmd)

    def test_verify_fresh_uptime_reports_ok(self):
        texts = relay_texts()
        texts['info_text'] = texts['info_text'].replace('|4000', '|3000')
        result = flash_and_verify('/dev/cu.a', 'esp32c5-eth', self.project,
                                  self.cache, self.runner(),
                                  probe=self.verify_probe(texts), boot_s=0)
        self.assertTrue(result['ok'])
        self.assertTrue(result['fresh'])

    def test_verify_old_uptime_fails_with_uptime_error(self):
        old = relay_texts()
        old['info_text'] = old['info_text'].replace('|4000', '|60000')
        result = flash_and_verify('/dev/cu.a', 'esp32c5-eth', self.project,
                                  self.cache, self.runner(),
                                  probe=self.verify_probe(old), boot_s=0)
        self.assertFalse(result['ok'])
        self.assertIn('uptime', result['error'])

    def test_verify_no_info_reply_fails(self):
        result = flash_and_verify('/dev/cu.a', 'esp32c5-eth', self.project,
                                  self.cache, self.runner(),
                                  probe=self.verify_probe(
                                      {'info_text': '', 'net_text': '',
                                       'eth_text': ''}),
                                  boot_s=0)
        self.assertFalse(result['ok'])
        self.assertIn('INFO', result['error'])

    def test_verify_probe_failure_fails_without_crash(self):
        def bad_probe(port_path, **kw):
            raise ProbeError('/dev/cu.a: gone')
        result = flash_and_verify('/dev/cu.a', 'esp32c5-eth', self.project,
                                  self.cache, self.runner(),
                                  probe=bad_probe, boot_s=0)
        self.assertFalse(result['ok'])
        self.assertIn('/dev/cu.a', result['error'])


class PioAvailable(unittest.TestCase):
    def test_existing_path_is_available(self):
        self.assertTrue(pio_available(os.path.realpath(__file__)))

    def test_missing_path_is_unavailable(self):
        self.assertFalse(pio_available('/nonexistent/pio/path'))


if __name__ == '__main__':
    unittest.main()
