'''RH-ENE 增量4：netplan——面向 W5500 模块的空闲 IP 与网关地址推导（纯函数）。

算法与两条格式坑照主 spec §2.1：判据按子网不按网卡（同卡双地址）、macOS
netmask 十六进制归一、ARP MAC 省略前导零归一、W5500 旧 MAC 条目排除、
无候选段抛错且探针零调用（零下发）。探针以 callable 注入，本测试不碰网络。
运行：cd src/tests && python -m unittest test_ethernet_usb_flash

覆盖面：usb_devices（INFO 解析/五类分类）+ netplan（空闲 IP/网关地址推导）。
'''

import os
import re
import sys
import unittest
from types import SimpleNamespace

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)
sys.path.append(os.path.join(_SRC_DIR, 'server'))
sys.path.append(os.path.join(_SRC_DIR, 'interface'))

from plugins.rh_usb_flash.firmware_flash import (FLASH_ENVS, FlashError,
                                           build_firmware, flash_firmware,
                                           override_ini_ip)
from plugins.rh_usb_flash.netplan import (NoCandidateSubnet, alive_probe, arp_entries,
                                    interface_env, normalize_mac,
                                    normalize_netmask, parse_arp,
                                    parse_default_gateway, parse_ifconfig,
                                    pick_free_ip, pick_gateway_host)
from plugins.rh_usb_flash.provision import (plan_relay_commands, provision_relay,
                                      relay_target_url)
from plugins.rh_usb_flash.usb_devices import classify_device, parse_info_line
from plugins.rh_usb_flash.usb_probe import ProbeError, probe_device

INFO_C5 = ('INFO widscan58|esp32c5|1|0.9.1-eth|2026-11-10|wifi6|'
           'de:ad:be:ef:00:55|123456\n')
INFO_S3 = ('INFO widscan58|esp32s3|1|0.9.0|2026-11-08|wifi|'
           'aa:bb:cc:dd:ee:01|900\n')
NET_STATUS_REPLY = ('NET {"url":"ws://192.168.31.102:8011/node",'
                    '"ssid":"HomeWiFi","on":true,"sta":3,"ws":1,'
                    '"ip4":"192.168.31.119","rssi":-52,"head_ready":true,'
                    '"head_baud":921600}\n')
ERR_REPLY = 'ERR command\n'
W5500_MAC = 'de:ad:be:ef:00:55'


class InfoParsing(unittest.TestCase):
    def test_full_line_parses_all_fields(self):
        info = parse_info_line(INFO_C5)
        self.assertEqual('esp32c5', info['target'])
        self.assertEqual(1, info['proto'])
        self.assertEqual('0.9.1-eth', info['version'])
        self.assertEqual('2026-11-10', info['commit'])
        self.assertEqual('wifi6', info['phy'])
        self.assertEqual('de:ad:be:ef:00:55', info['mac'])
        self.assertEqual(123456, info['uptime_ms'])

    def test_last_info_line_wins_when_boot_log_precedes(self):
        text = 'ESP-ROM:esp32c5-20210319\nsome boot noise\n' + INFO_S3
        info = parse_info_line(text)
        self.assertEqual('esp32s3', info['target'])

    def test_truncated_line_is_rejected(self):
        self.assertIsNone(parse_info_line('INFO widscan58|esp32c5|1\n'))

    def test_no_info_line_is_rejected(self):
        self.assertIsNone(parse_info_line('garbage\r\nERR command\n'))

    def test_non_numeric_uptime_is_rejected(self):
        bad = ('INFO widscan58|esp32c5|1|v|d|wifi|de:ad:be:ef:00:55|xyz\n')
        self.assertIsNone(parse_info_line(bad))


class Classification(unittest.TestCase):
    def probe(self, info, net=ERR_REPLY, eth=ERR_REPLY):
        return classify_device(info, net, eth)

    def test_relay_when_s3_answers_net_status(self):
        d = self.probe(parse_info_line(INFO_S3), net=NET_STATUS_REPLY)
        self.assertEqual('relay', d['kind'])
        self.assertEqual('esp32s3-relay', d['flash_default'])
        self.assertTrue(d['provisionable'])
        self.assertEqual('ws://192.168.31.102:8011/node',
                         d['net_status']['url'])
        self.assertEqual('192.168.31.119', d['net_status']['ip4'])

    def test_bare_s3_without_net_reply_flashes_relay(self):
        d = self.probe(parse_info_line(INFO_S3))
        self.assertEqual('s3', d['kind'])
        self.assertEqual('esp32s3-relay', d['flash_default'])
        self.assertFalse(d['provisionable'])

    def test_eth_when_c5_answers_ethip(self):
        info = parse_info_line(INFO_C5)
        d = self.probe(info, eth='ETHIP 192.168.0.98 255.255.255.0 0.0.0.0 '
                                 'compile\n')
        self.assertEqual('eth', d['kind'])
        self.assertEqual('esp32c5-eth', d['flash_default'])
        self.assertTrue(d['provisionable'])

    def test_bare_c5_is_flashed_as_eth_by_default(self):
        d = self.probe(parse_info_line(INFO_C5))
        self.assertEqual('c5', d['kind'])
        self.assertEqual('esp32c5-eth', d['flash_default'])
        self.assertFalse(d['provisionable'])

    def test_unknown_when_info_silent(self):
        d = self.probe(None, net=NET_STATUS_REPLY)
        self.assertEqual('unknown', d['kind'])
        self.assertIsNone(d['flash_default'])
        self.assertFalse(d['provisionable'])

    def test_unparsable_info_falls_to_unknown(self):
        d = self.probe(parse_info_line('INFO widscan58|esp32c5|1\n'))
        self.assertEqual('unknown', d['kind'])

    def test_info_fields_are_carried_through(self):
        d = self.probe(parse_info_line(INFO_C5))
        self.assertEqual('de:ad:be:ef:00:55', d['mac'])
        self.assertEqual(123456, d['uptime_ms'])
        self.assertEqual('0.9.1-eth', d['version'])
        self.assertEqual(INFO_C5.strip(), d['info_raw'])

    def test_s3_relay_classification_ignores_eth_probe_entirely(self):
        d = self.probe(parse_info_line(INFO_S3), net=NET_STATUS_REPLY,
                       eth='ETHIP 192.168.0.98 255.255.255.0 0.0.0.0 nvs\n')
        self.assertEqual('relay', d['kind'])


class Normalization(unittest.TestCase):
    def test_macos_hex_netmask_becomes_prefix(self):
        self.assertEqual(24, normalize_netmask('0xffffff00'))

    def test_dotted_netmask_becomes_prefix(self):
        self.assertEqual(24, normalize_netmask('255.255.255.0'))

    def test_mac_with_missing_leading_zeros_is_normalized(self):
        self.assertEqual('46:90:c4:02:b5:0c',
                         normalize_mac('46:90:c4:2:b5:c'))

    def test_w5500_mac_normalization_is_stable(self):
        self.assertEqual(W5500_MAC, normalize_mac('de:ad:be:ef:0:55'))


class SubnetSelection(unittest.TestCase):
    def interfaces(self, ifaces, default_gw):
        return {'interfaces': ifaces, 'default_gateway': default_gw}

    def test_same_nic_dual_address_keeps_alias_subnet(self):
        # 2026-11-14 实测本机形态：en0 同时挂默认路由段与面向 W5500 的别名段。
        env = self.interfaces([
            {'iface': 'en0', 'ipv4': '192.168.31.102', 'netmask': '0xffffff00'},
            {'iface': 'en0', 'ipv4': '192.168.0.100', 'netmask': '0xffffff00'},
            {'iface': 'utun3', 'ipv4': '100.82.23.66', 'netmask': '0xfffffc00'},
        ], '192.168.31.1')
        probe_calls = []
        ip = pick_free_ip(env, [], lambda a: probe_calls.append(a) or False)
        self.assertTrue(ip.startswith('192.168.0.'))
        self.assertFalse(ip.startswith('192.168.31.'))
        self.assertFalse(ip.startswith('100.82.'))

    def test_skips_occupied_and_local_addresses(self):
        env = self.interfaces([
            {'iface': 'en0', 'ipv4': '192.168.0.100', 'netmask': '0xffffff00'},
        ], '192.168.31.1')
        arp = {'192.168.0.98': W5500_MAC, '192.168.0.99': '68:da:73:aa:bb:01'}
        # .98=W5500 旧条目、.99=别的设备、.100=本机自身 → 从 .101 起。
        self.assertEqual('192.168.0.101', pick_free_ip(env, arp, lambda a: False))

    def test_ping_answer_takes_precedence_after_arp(self):
        env = self.interfaces([
            {'iface': 'en0', 'ipv4': '192.168.0.100', 'netmask': '0xffffff00'},
        ], '192.168.31.1')
        self.assertEqual('192.168.0.99', pick_free_ip(
            env, {}, lambda a: a == '192.168.0.98'))

    def test_no_private_subnet_raises_and_probe_never_called(self):
        env = self.interfaces([
            {'iface': 'utun3', 'ipv4': '100.82.23.66', 'netmask': '0xfffffc00'},
        ], '192.168.31.1')
        calls = []
        with self.assertRaises(NoCandidateSubnet):
            pick_free_ip(env, [], lambda a: calls.append(a) or False)
        self.assertEqual([], calls)

    def test_no_free_address_raises(self):
        env = self.interfaces([
            {'iface': 'en0', 'ipv4': '192.168.0.100', 'netmask': '0xffffff00'},
        ], '192.168.31.1')
        with self.assertRaises(NoCandidateSubnet):
            pick_free_ip(env, {}, lambda a: True)


class GatewayHostSelection(unittest.TestCase):
    SERVERS = ['192.168.31.102', '192.168.0.100', '100.82.23.66',
               '10.211.55.2', '10.37.129.2', '192.168.0.99']

    def test_relay_subnet_pick_from_broadcast_list(self):
        host, guessed = pick_gateway_host(self.SERVERS, '192.168.31.119',
                                          '192.168.0.100')
        self.assertEqual('192.168.31.102', host)
        self.assertFalse(guessed)

    def test_relay_without_ip4_falls_back_to_local_address_and_flags_guess(self):
        host, guessed = pick_gateway_host(self.SERVERS, None, '192.168.0.100')
        self.assertEqual('192.168.0.100', host)
        self.assertTrue(guessed)

    def test_no_same_subnet_broadcast_falls_back_and_flags_guess(self):
        host, guessed = pick_gateway_host(self.SERVERS, '172.16.5.9',
                                          '192.168.0.100')
        self.assertEqual('192.168.0.100', host)
        self.assertTrue(guessed)


MAC_IFCONFIG = '''lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.31.102 netmask 0xffffff00 broadcast 192.168.31.255
\tinet 192.168.0.100 netmask 0xffffff00 broadcast 192.168.0.255
utun3: flags=8051<UP,POINTOPOINT,RUNNING,NOARP,MULTICAST> mtu 1380
\tinet 100.82.23.66 --> 100.82.23.66 netmask 0xfffffc00
'''

MAC_NETSTAT = '''Routing tables

Internet:
Destination        Gateway            Flags        Refs      Use   Netif Expire
default            192.168.31.1       UGSc           45        0     en0
'''

LINUX_IP_ADDR = '''2: enp3s0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq state UP
    inet 192.168.1.10/24 brd 192.168.1.255 scope global dynamic enp3s0
3: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420 qdisc noqueue state UNKNOWN
    inet 10.8.0.2/24 scope global wg0
'''

LINUX_IP_ROUTE = 'default via 192.168.31.1 dev enp3s0 proto dhcp metric 100\n'


class InterfaceCollection(unittest.TestCase):
    def test_mac_ifconfig_yields_entries_with_hex_netmask(self):
        entries = parse_ifconfig(MAC_IFCONFIG)
        self.assertEqual([{'iface': 'en0', 'ipv4': '192.168.31.102',
                           'netmask': '0xffffff00'},
                          {'iface': 'en0', 'ipv4': '192.168.0.100',
                           'netmask': '0xffffff00'},
                          {'iface': 'utun3', 'ipv4': '100.82.23.66',
                           'netmask': '0xfffffc00'}], entries)

    def test_linux_ip_addr_yields_entries_with_prefix_netmask(self):
        entries = parse_ifconfig(LINUX_IP_ADDR, platform='linux')
        self.assertEqual([{'iface': 'enp3s0', 'ipv4': '192.168.1.10',
                           'netmask': '255.255.255.0'},
                          {'iface': 'wg0', 'ipv4': '10.8.0.2',
                           'netmask': '255.255.255.0'}], entries)

    def test_default_gateway_parses_netstat_and_ip_route(self):
        self.assertEqual('192.168.31.1',
                         parse_default_gateway(MAC_NETSTAT))
        self.assertEqual('192.168.31.1',
                         parse_default_gateway(LINUX_IP_ROUTE))
        self.assertIsNone(parse_default_gateway('garbage'))

    def test_interface_env_combines_both_lookups(self):
        outputs = {'ifconfig -a': MAC_IFCONFIG,
                   'netstat -rn -f inet': MAC_NETSTAT}

        def runner(cmd):
            return outputs[cmd]

        env = interface_env(runner=runner, platform='darwin')
        self.assertEqual('192.168.31.1', env['default_gateway'])
        self.assertEqual(3, len(env['interfaces']))

    def test_interface_env_linux_uses_ip_commands(self):
        outputs = {'ip -4 addr show': LINUX_IP_ADDR,
                   'ip route show': LINUX_IP_ROUTE}

        def runner(cmd):
            return outputs[cmd]

        env = interface_env(runner=runner, platform='linux')
        self.assertEqual('192.168.31.1', env['default_gateway'])
        self.assertEqual(2, len(env['interfaces']))


MAC_ARP = ('? (192.168.0.98) at de:ad:be:ef:0:55 on en0 ifscope [ethernet]\n'
           '? (192.168.0.1) at 68:da:73:aa:bb:1 on en0 ifscope [ethernet]\n'
           '? (192.168.0.102) at incomplete on en0 ifscope [ethernet]\n')

LINUX_ARP = ('192.168.0.98 ether de:ad:be:ef:00:55 eth0\n'
             '? (192.168.0.1) at 68:da:73:aa:bb:1 [ether] on eth0\n')


class ArpCollection(unittest.TestCase):
    def test_mac_arp_output_parses_with_zero_padding(self):
        self.assertEqual({'192.168.0.98': 'de:ad:be:ef:00:55',
                          '192.168.0.1': '68:da:73:aa:bb:01'},
                         parse_arp(MAC_ARP))

    def test_linux_arp_output_parses(self):
        self.assertEqual({'192.168.0.98': 'de:ad:be:ef:00:55',
                          '192.168.0.1': '68:da:73:aa:bb:01'},
                         parse_arp(LINUX_ARP))

    def test_arp_entries_runner_returns_dict(self):
        env = arp_entries(runner=lambda cmd: MAC_ARP, platform='darwin')
        self.assertIn('192.168.0.98', env)


class AliveProbe(unittest.TestCase):
    def test_ping_zero_return_code_means_alive(self):
        calls = []

        def runner(cmd):
            calls.append(cmd)
            return 0 if cmd[-1] == '192.168.0.98' else 1

        alive = alive_probe(runner=runner, platform='darwin')
        self.assertTrue(alive('192.168.0.98'))
        self.assertFalse(alive('192.168.0.99'))
        self.assertEqual(['ping', '-c', '1', '-W', '1000', '192.168.0.98'],
                         calls[0])

    def test_runner_exception_means_not_alive(self):
        def runner(cmd):
            raise OSError('no ping')

        self.assertFalse(alive_probe(runner=runner,
                                     platform='linux')('1.2.3.4'))


class FirmwareFlash(unittest.TestCase):
    '''命令拼装层：runner 全部注入假对象，不真构建不真烧录。

    IP 注入的唯一可行路径是「完整 ini 副本 + pio run -c」（2026-11-15 实测：
    PLATFORMIO_BUILD_FLAGS 追加会与 env 已有宏重复定义，-Werror 拒编）。
    '''

    def setUp(self):
        import tempfile
        self.proj = tempfile.mkdtemp(prefix='fw_proj_')
        # 假工程：platformio.ini 含 esp32c5-eth 的 IP 行（与真工程相同写法）。
        with open(os.path.join(self.proj, 'platformio.ini'), 'w') as f:
            f.write('[env:esp32c5-eth]\n'
                    'extends = env:esp32c5\n'
                    'build_flags = -DRX_TARGET_C5=1 -DRX_W5500=1 '
                    "-DETH_IP_ADDR='\"192.168.0.98\"' "
                    "-DETH_IP_GATEWAY='\"0.0.0.0\"' "
                    "-DETH_IP_NETMASK='\"255.255.255.0\"'\n"
                    '[env:esp32s3-relay]\n'
                    'extends = env:esp32s3\n'
                    'build_flags = -DRX_TARGET_S3=1 -DRX_WIFI_RELAY=1\n')

    def records(self):
        import tempfile
        return tempfile.mkdtemp(prefix='fw_cache_')

    def fake_runner(self, argv_text=''):
        calls = []
        captures = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            # 覆盖 ini 用后即删，须在 runner 被调瞬间捕获其内容。
            match = re.search(r'-c (\S+)', cmd)
            if not match:
                captures.append(None)
            else:
                with open(match.group(1)) as ini:
                    captures.append(ini.read())
            # 真实 runner 的语义是「执行构建并产出产物」：按 argv 的 -e env
            # 在 cwd 下产出 firmware.bin，模拟 PIO 的成功路径。
            env_match = re.search(r'-e (\S+)', cmd)
            if env_match and kwargs.get('cwd'):
                bin_dir = os.path.join(kwargs['cwd'], '.pio', 'build',
                                       env_match.group(1))
                os.makedirs(bin_dir, exist_ok=True)
                with open(os.path.join(bin_dir, 'firmware.bin'), 'wb') as f:
                    f.write(b'fake-firmware')
            return SimpleNamespace(returncode=0, stdout=argv_text, stderr='')
        return run, calls, captures

    def test_env_catalog_lists_all_four_targets(self):
        self.assertEqual({'esp32c5', 'esp32c5-head', 'esp32c5-eth',
                          'esp32s3-relay'}, set(FLASH_ENVS))

    def test_eth_build_rewrites_ip_in_copy_ini_and_runs_with_c_flag(self):
        run, calls, _cap = self.fake_runner()
        cache = self.records()
        result = build_firmware('esp32c5-eth', self.proj, cache,
                                runner=run, injected_ip='192.168.0.123')
        self.assertEqual(1, len(calls))
        cmd = calls[0]
        self.assertIn('-e esp32c5-eth', cmd)
        self.assertIn('-c ', cmd)
        ini_path = re.search(r'-c (\S+)', cmd).group(1)
        self.assertIn('.pio', ini_path)
        self.assertIn('"192.168.0.123"', _cap[0])
        self.assertNotIn('"192.168.0.98"', _cap[0])
        self.assertEqual('192.168.0.123', result['injected_ip'])

    def test_non_injected_build_runs_main_ini_without_copy(self):
        run, calls, _cap = self.fake_runner()
        result = build_firmware('esp32s3-relay', self.proj, self.records(),
                                runner=run)
        self.assertEqual(1, len(calls))
        self.assertNotIn('-c', calls[0])
        self.assertIsNone(result['injected_ip'])

    def test_cache_hit_skips_runner_and_reports_hit(self):
        run, calls, _cap = self.fake_runner()
        cache = self.records()
        first = build_firmware('esp32c5-eth', self.proj, cache, runner=run,
                               injected_ip='192.168.0.123')
        run2, calls2, _cap2 = self.fake_runner()
        second = build_firmware('esp32c5-eth', self.proj, cache, runner=run2,
                                injected_ip='192.168.0.123')
        self.assertEqual([], calls2)
        self.assertTrue(second['cache_hit'])
        self.assertEqual(first['bin'], second['bin'])

    def test_different_ip_never_shares_cache(self):
        run, _, _cap = self.fake_runner()
        cache = self.records()
        build_firmware('esp32c5-eth', self.proj, cache, runner=run,
                       injected_ip='192.168.0.123')
        run2, calls2, _cap2 = self.fake_runner()
        result = build_firmware('esp32c5-eth', self.proj, cache, runner=run2,
                                injected_ip='192.168.0.99')
        self.assertEqual(1, len(calls2))
        self.assertFalse(result['cache_hit'])

    def test_build_failure_raises_with_log_tail(self):
        def failing_run(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stdout='', stderr='cc1: error')
        with self.assertRaises(FlashError) as ctx:
            build_firmware('esp32s3-relay', self.proj, self.records(),
                           runner=failing_run)
        self.assertIn('cc1: error', str(ctx.exception))

    def test_missing_project_dir_fails_before_runner(self):
        run, calls, _cap = self.fake_runner()
        with self.assertRaises(FlashError):
            build_firmware('esp32s3-relay', '/nonexistent/firmware',
                           self.records(), runner=run)
        self.assertEqual([], calls)

    def test_flash_builds_then_uploads_to_port(self):
        run, calls, _cap = self.fake_runner()
        cache = self.records()
        flash_firmware('esp32s3-relay', '/dev/cu.fake', self.proj, cache,
                       runner=run, injected_ip=None)
        self.assertEqual(2, len(calls))
        upload_cmd = calls[1]
        self.assertIn('-t upload', upload_cmd)
        self.assertIn('--upload-port /dev/cu.fake', upload_cmd)

    def test_override_ini_ip_only_touches_target_env_block(self):
        text = ('[env:esp32c5-eth]\n'
                "build_flags = -DETH_IP_ADDR='\"192.168.0.98\"'\n"
                '[env:esp32c5-eth-probe]\n'
                "build_flags = -DETH_IP_ADDR='\"192.168.0.98\"'\n")
        out = override_ini_ip(text, 'esp32c5-eth', '192.168.0.123')
        # 只改 [env:esp32c5-eth] 段内的行；探针 env（别人 extends 自它）不动。
        eth_block = out.split('[env:esp32c5-eth-probe]')[0]
        self.assertIn('192.168.0.123', eth_block)
        self.assertIn('192.168.0.98', out.split('[env:esp32c5-eth-probe]')[1])

    def test_target_env_absent_from_ini_fails_clean(self):
        with self.assertRaises(FlashError):
            override_ini_ip('[env:other]\nbuild_flags =\n',
                            'esp32c5-eth', '192.168.0.123')


class UsbProbe(unittest.TestCase):
    '''no-reset 串口探针层：serial 注入假对象，不碰真设备。

    电平纪律照 tools/real_laps_c/c5_usb_probe.py：dtr=True rts=False（运行态，
    C5 rev≤1.0 上拉 RTS 会打死板子），open 后不做任何复位。
    '''

    def fake_serial(self, replies):
        '''replies: 命令行 → 应答文本；未列出的命令无应答。'''
        opened, written = [], []

        class FakeSerial(object):
            def __init__(self, port, baud, timeout=None):
                self.port = port
                self.dtr = None
                self.rts = None
                self._pending = b''
                opened.append(port)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __enter_done(self):
                pass

            def reset_input_buffer(self):
                pass

            @property
            def in_waiting(self):
                return len(self._pending)

            def read(self, n):
                out, self._pending = self._pending[:n], self._pending[n:]
                return out

            def write(self, data):
                written.append(data.decode())
                self._pending = replies.get(
                    data.decode().strip(), '').encode()

            def flush(self):
                pass

            def close(self):
                pass

        return FakeSerial, opened, written

    def test_opens_without_reset_and_sends_three_probes(self):
        factory, opened, written = self.fake_serial({
            'INFO': INFO_S3,
            'NET STATUS': NET_STATUS_REPLY,
            'ETHIP?': ERR_REPLY,
        })
        result = probe_device('/dev/cu.fake', serial_factory=factory, respond_s=0.05)
        self.assertEqual(['/dev/cu.fake'], opened)
        self.assertEqual(['INFO\n', 'NET STATUS\n', 'ETHIP?\n'], written)
        self.assertIn('esp32s3', result['info_text'])

    def test_silent_device_returns_empty_texts(self):
        factory, opened, written = self.fake_serial({})
        result = probe_device('/dev/cu.fake', serial_factory=factory, respond_s=0.05)
        self.assertEqual('', result['info_text'])
        self.assertEqual('', result['net_text'])
        self.assertEqual('', result['eth_text'])

    def test_open_failure_raises_probe_error(self):
        def bad_factory(*a, **k):
            raise IOError('Device busy')
        with self.assertRaises(ProbeError) as ctx:
            probe_device('/dev/cu.fake', serial_factory=bad_factory)
        self.assertIn('/dev/cu.fake', str(ctx.exception))


class RelayProvision(unittest.TestCase):
    '''S3 中继「直接配网」编排（主 spec §2.2 链路；网关侧只 GET——红线）。'''

    def serial_of(self, script):
        '''script: {'NET URL ...': reply, ...};未列命令回 ERR。'''
        factory, _opened, written = self._fake(script)
        return factory, written

    def _fake(self, script):
        written = []

        class FakeSerial(object):
            def __init__(self, port, baud, timeout=None):
                pass

            def reset_input_buffer(self):
                pass

            @property
            def in_waiting(self):
                return len(self._pending)

            def read(self, n):
                out, self._pending = self._pending[:n], self._pending[n:]
                return out

            def write(self, data):
                text = data.decode().strip()
                written.append(text)
                self._pending = script.get(text, 'ERR command\n').encode()

            def flush(self):
                pass

            def close(self):
                pass

            _pending = b''
        return FakeSerial, written

    def status(self, url):
        return ('NET {{"url":"{}","on":true,"sta":3,"ws":1,'
                '"ip4":"192.168.31.119","rssi":-52}}\n').format(url)

    def run_case(self, script, gateway=None):
        factory, written = self._fake(script)
        polls = {'n': 0}

        def nodes(url):
            polls['n'] += 1
            return gateway(polls['n']) if gateway else False
        result = provision_relay('/dev/cu.fake', '192.168.31.102', 8011,
                                 serial_factory=factory, respond_s=0.05,
                                 post_s=0.05, gateway_nodes=nodes)
        return result, written

    def test_plan_idempotent_when_already_pointing_at_target(self):
        self.assertEqual([], plan_relay_commands(
            {'url': 'ws://192.168.31.102:8011/node'}, '192.168.31.102', 8011))
        self.assertEqual([], plan_relay_commands(
            {'url': '192.168.31.102:8011'}, '192.168.31.102', 8011))

    def test_plan_emits_url_and_save_when_pointing_elsewhere(self):
        self.assertEqual(['NET URL ws://192.168.31.102:8011/node',
                          'NET SAVE'],
                         plan_relay_commands({'url': 'ws://10.0.0.5:8011/node'},
                                             '192.168.31.102', 8011))

    def test_full_flow_when_config_changed(self):
        script = {
            'NET STATUS': self.status('ws://10.0.0.5:8011/node'),
            'NET URL ws://192.168.31.102:8011/node': 'OK\n',
            'NET SAVE': 'OK\n',
            'NET OFF': 'OK\n',
            'NET ON': 'OK\n',
        }
        result, written = self.run_case(script, gateway=lambda n: n >= 2)
        self.assertTrue(result['ok'])
        self.assertEqual(['NET STATUS', 'NET URL ws://192.168.31.102:8011/node',
                          'NET SAVE', 'NET STATUS', 'NET OFF', 'NET ON'],
                         written)
        self.assertTrue(result['online'])

    def test_readback_mismatch_stops_before_off_on(self):
        script = {
            'NET STATUS': self.status('ws://10.0.0.5:8011/node'),
            'NET URL ws://192.168.31.102:8011/node': 'ERR net_url url\n',
        }
        result, written = self.run_case(script)
        self.assertFalse(result['ok'])
        self.assertNotIn('NET OFF', written)
        self.assertIn('net_url', result['error'])

    def test_no_status_reply_sends_no_write_commands(self):
        script = {'NET STATUS': ''}
        result, written = self.run_case(script)
        self.assertFalse(result['ok'])
        self.assertEqual(['NET STATUS'], written)

    def test_idempotent_flow_skips_writes_but_still_relinks(self):
        script = {
            'NET STATUS': self.status('ws://192.168.31.102:8011/node'),
            'NET OFF': 'OK\n',
            'NET ON': 'OK\n',
        }
        result, written = self.run_case(script, gateway=lambda n: True)
        self.assertTrue(result['ok'])
        self.assertNotIn('NET URL ws://192.168.31.102:8011/node', written)
        self.assertIn('NET ON', written)
        self.assertTrue(result['online'])

    def test_gateway_never_returns_online_reports_false_not_failure(self):
        script = {
            'NET STATUS': self.status('ws://10.0.0.5:8011/node'),
            'NET URL ws://192.168.31.102:8011/node': 'OK\n',
            'NET SAVE': 'OK\n',
            'NET OFF': 'OK\n',
            'NET ON': 'OK\n',
        }
        result, written = self.run_case(script, gateway=lambda n: False)
        self.assertTrue(result['ok'])
        self.assertFalse(result['online'])

    def test_target_url_shape(self):
        self.assertEqual('ws://192.168.31.102:8011/node',
                         relay_target_url('192.168.31.102', 8011))


if __name__ == '__main__':
    unittest.main()
