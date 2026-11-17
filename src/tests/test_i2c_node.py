'''i2c_node：I2C 传输层与 discover 扫描的单元测试（不碰真 I2C 硬件）。

I2CNode 通过 i2c_helper 读写 block，所以测试注入 FakeI2C（按 (addr, command)
回放结果、可注入 IOError）与 FakeInterface（收集日志与计数）；discover 的
地址扫描用 FakeI2C 模拟「哪些地址有节点」，节点自身的 read_block 用 mock
隔离，避免把扫描逻辑和数据协议耦合在一起。

运行：cd src/tests && python -m unittest test_i2c_node -v
（该套件 import RHInterface，需要带 gevent 的解释器，如项目 .venv-rh）
'''

import os
import sys
import unittest
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)
sys.path.append(os.path.join(_SRC_DIR, 'server'))
sys.path.append(os.path.join(_SRC_DIR, 'interface'))

import i2c_node
from i2c_node import I2CNode
from RHInterface import calculate_checksum

ADDR = 8


def with_checksum(payload):
    payload = bytearray(payload)
    payload.append(calculate_checksum(payload))
    return payload


class FakeI2C:
    '''替身：read 按 (addr, command) 队列回放（元素可为异常），write 记账。'''

    def __init__(self):
        self.i2c = self
        self.i2c_timestamp = 0.0
        self.read_plan = {}
        self.default_read = IOError('no device')
        self.writes = []
        self.write_outcomes = []
        self.end_count = 0
        self.sleep_count = 0

    def read_i2c_block_data(self, addr, command, size):
        plan = self.read_plan.get((addr, command))
        if not plan:
            raise self.default_read
        result = plan.pop(0) if len(plan) > 1 else plan[0]
        if isinstance(result, Exception):
            raise result
        return list(result)

    def write_i2c_block_data(self, addr, command, data):
        self.writes.append((addr, command, list(data)))

    def with_i2c(self, fn):
        if self.write_outcomes:
            outcome = self.write_outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is None:
                return None
        return fn()

    def i2c_end(self):
        self.end_count += 1

    def i2c_sleep(self):
        self.sleep_count += 1


class FakeInterface:
    def __init__(self):
        self.logs = []
        self.read_blocks = 0
        self.read_errors = 0
        self.write_blocks = 0
        self.write_errors = 0

    def log(self, message):
        self.logs.append(message)

    def inc_intf_read_block_count(self):
        self.read_blocks += 1

    def inc_intf_read_error_count(self):
        self.read_errors += 1

    def inc_intf_write_block_count(self):
        self.write_blocks += 1

    def inc_intf_write_error_count(self):
        self.write_errors += 1


class Base(unittest.TestCase):
    def setUp(self):
        self.i2c = FakeI2C()
        self.interface = FakeInterface()
        self.node = I2CNode(0, ADDR, self.i2c)


class ReadBlockTests(Base):
    def test_valid_block_returns_payload_and_counts_block(self):
        self.i2c.read_plan[(ADDR, 0x03)] = [with_checksum(b'\x07\x08')]
        self.assertEqual(bytes(self.node.read_block(self.interface, 0x03, 2)),
                         b'\x07\x08')
        self.assertEqual(self.node.read_block_count, 1)
        self.assertEqual(self.node.read_error_count, 0)
        self.assertEqual(self.interface.read_blocks, 1)
        self.assertEqual(self.interface.logs, [])

    def test_bad_checksum_retries_then_succeeds(self):
        self.i2c.read_plan[(ADDR, 0x03)] = [
            with_checksum(b'\x07\x08')[:-1] + bytearray(b'\xFF'),
            with_checksum(b'\x07\x08')]
        self.assertEqual(bytes(self.node.read_block(self.interface, 0x03, 2)),
                         b'\x07\x08')
        self.assertEqual(self.node.read_error_count, 1)
        self.assertEqual(self.interface.read_errors, 1)
        # 首次重试不写日志（避免偶发单次重试刷屏）
        self.assertEqual(self.interface.logs, [])

    def test_checksum_never_valid_returns_none(self):
        bad = with_checksum(b'\x07\x08')[:-1] + bytearray(b'\xFF')
        self.i2c.read_plan[(ADDR, 0x03)] = [bad]
        self.assertIsNone(
            self.node.read_block(self.interface, 0x03, 2, max_retries=1))
        self.assertEqual(self.node.read_error_count, 2)
        self.assertTrue(any('limit reached' in log
                            for log in self.interface.logs))

    def test_io_error_ends_i2c_and_returns_none(self):
        self.i2c.read_plan[(ADDR, 0x03)] = [IOError('bus down')]
        self.assertIsNone(
            self.node.read_block(self.interface, 0x03, 2, max_retries=0))
        self.assertEqual(self.i2c.end_count, 1)
        self.assertIn('Read Error: bus down', self.interface.logs)
        self.assertEqual(self.node.read_error_count, 1)


class WriteBlockTests(Base):
    def test_api_20_appends_only_checksum(self):
        self.node.api_level = 20
        self.assertTrue(self.node.write_block(self.interface, 0x51,
                                              bytearray(b'\x01\x02')))
        payload = bytearray(b'\x01\x02')
        payload.append(calculate_checksum(payload))
        self.assertEqual(self.i2c.writes, [(ADDR, 0x51, list(payload))])
        self.assertEqual(self.interface.write_blocks, 1)

    def test_api_19_appends_command_before_checksum(self):
        self.node.api_level = 19
        self.assertTrue(self.node.write_block(self.interface, 0x51,
                                              bytearray(b'\x01\x02')))
        payload = bytearray(b'\x01\x02\x51')
        payload.append(calculate_checksum(payload))
        self.assertEqual(self.i2c.writes, [(ADDR, 0x51, list(payload))])

    def test_none_from_with_i2c_is_treated_as_failure_and_retried(self):
        self.node.api_level = 20
        self.i2c.write_outcomes = [None, True]
        self.assertTrue(self.node.write_block(self.interface, 0x51,
                                              bytearray(b'\x01')))
        self.assertEqual(len(self.i2c.writes), 1)

    def test_io_error_retries_then_succeeds(self):
        self.node.api_level = 20
        self.i2c.write_outcomes = [IOError('nack'), True]
        self.assertTrue(self.node.write_block(self.interface, 0x51,
                                              bytearray(b'\x01')))
        self.assertEqual(self.i2c.end_count, 1)
        self.assertEqual(self.interface.write_errors, 1)
        self.assertEqual(len(self.i2c.writes), 1)

    def test_all_retries_fail_returns_false(self):
        self.node.api_level = 20
        self.i2c.write_outcomes = [IOError('nack')] * 99
        self.assertFalse(self.node.write_block(self.interface, 0x51,
                                               bytearray(b'\x01')))
        self.assertEqual(self.interface.write_errors,
                         i2c_node.MAX_RETRY_COUNT + 1)
        self.assertTrue(any('limit reached' in log
                            for log in self.interface.logs))


class FirmwareStringTests(Base):
    def test_read_firmware_version_decodes_and_strips_nulls(self):
        self.node.read_block = mock.Mock(return_value=bytearray(b'4.6.0\0\0'))
        self.node.read_firmware_version()
        self.assertEqual(self.node.firmware_version_str, '4.6.0')

    def test_read_firmware_version_none_when_block_missing(self):
        self.node.read_block = mock.Mock(return_value=None)
        self.node.read_firmware_version()
        self.assertIsNone(self.node.firmware_version_str)

    def test_read_firmware_version_swallows_exceptions(self):
        self.node.read_block = mock.Mock(side_effect=RuntimeError('boom'))
        with self.assertLogs('i2c_node', level='ERROR'):
            self.node.read_firmware_version()
        self.assertIsNone(self.node.firmware_version_str)

    def test_read_firmware_proctype(self):
        self.node.read_block = mock.Mock(return_value=bytearray(b'ESP32\0'))
        self.node.read_firmware_proctype()
        self.assertEqual(self.node.firmware_proctype_str, 'ESP32')

    def test_read_firmware_timestamp_joins_date_and_time(self):
        self.node.read_block = mock.Mock(
            side_effect=[bytearray(b'Nov 16 2026\0'),
                         bytearray(b'16:37:00\0')])
        self.node.read_firmware_timestamp()
        self.assertEqual(self.node.firmware_timestamp_str,
                         'Nov 16 2026 16:37:00')
        self.assertEqual(self.node.read_block.call_count, 2)

    def test_read_firmware_timestamp_none_when_date_missing(self):
        self.node.read_block = mock.Mock(return_value=None)
        self.node.read_firmware_timestamp()
        self.assertIsNone(self.node.firmware_timestamp_str)
        self.assertEqual(self.node.read_block.call_count, 1)

    def test_jump_to_bootloader_is_noop_for_i2c(self):
        self.assertIsNone(self.node.jump_to_bootloader(self.interface))


class SendStatusMessageTests(Base):
    def test_writes_packed_message_from_api_35(self):
        self.node.api_level = 35
        self.node.write_block = mock.Mock(return_value=True)
        self.assertTrue(self.node.send_status_message(self.interface, 2, 7))
        self.node.write_block.assert_called_once_with(
            self.interface, i2c_node.SEND_STATUS_MESSAGE,
            i2c_node.pack_16((2 << 8) | 7))

    def test_ignored_below_api_35(self):
        self.node.api_level = 34
        self.node.write_block = mock.Mock()
        self.assertFalse(self.node.send_status_message(self.interface, 2, 7))
        self.node.write_block.assert_not_called()


class DiscoverTests(unittest.TestCase):
    '''discover 只负责地址扫描与节点装配，read_block 用 mock 隔离。'''

    def setUp(self):
        self.i2c = FakeI2C()

    def revision(self, api_level=35, verify=0x25):
        return bytearray([verify, api_level])

    def test_no_devices_found_scans_all_addresses(self):
        nodes = i2c_node.discover(0, self.i2c)
        self.assertEqual(nodes, [])
        self.assertEqual(self.i2c.end_count, 8)
        self.assertEqual(self.i2c.sleep_count, 8)

    def test_s32_bpill_stops_after_first_missing_address(self):
        nodes = i2c_node.discover(0, self.i2c, isS32BPillFlag=True)
        self.assertEqual(nodes, [])
        self.assertEqual(self.i2c.end_count, 1)
        self.assertEqual(self.i2c.sleep_count, 1)

    def test_node_found_with_firmware_strings(self):
        self.i2c.read_plan[(ADDR, i2c_node.READ_ADDRESS)] = [[0x01]]
        with mock.patch.object(i2c_node.I2CNode, 'read_block',
                               side_effect=[self.revision(35),
                                            bytearray(b'4.6.0\0'),
                                            bytearray(b'ESP32\0'),
                                            bytearray(b'Nov 16 2026\0'),
                                            bytearray(b'16:37:00\0')]):
            with self.assertLogs('i2c_node', level='INFO') as logs:
                nodes = i2c_node.discover(0, self.i2c)
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node.index, 0)
        self.assertEqual(node.i2c_addr, ADDR)
        self.assertEqual(node.api_level, 35)
        self.assertEqual(node.firmware_version_str, '4.6.0')
        self.assertEqual(node.firmware_proctype_str, 'ESP32')
        self.assertEqual(node.firmware_timestamp_str, 'Nov 16 2026 16:37:00')
        self.assertIn(
            'I2C node 1 found at address 8, API_level=35, fw_version=4.6.0, '
            'fw_type=ESP32, fw_timestamp: Nov 16 2026 16:37:00',
            '\n'.join(logs.output))

    def test_idx_offset_applied_to_node_index(self):
        self.i2c.read_plan[(ADDR, i2c_node.READ_ADDRESS)] = [[0x01]]
        with mock.patch.object(i2c_node.I2CNode, 'read_block',
                               return_value=self.revision(34)):
            nodes = i2c_node.discover(2, self.i2c)
        self.assertEqual(nodes[0].index, 2)

    def test_unverified_revision_keeps_api_level_zero(self):
        self.i2c.read_plan[(ADDR, i2c_node.READ_ADDRESS)] = [[0x01]]
        with mock.patch.object(i2c_node.I2CNode, 'read_block',
                               return_value=self.revision(35, verify=0x24)):
            with self.assertLogs('i2c_node', level='WARNING') as logs:
                nodes = i2c_node.discover(0, self.i2c)
        self.assertEqual(nodes[0].api_level, 0)
        self.assertIn('Unable to verify revision code from node 1',
                      '\n'.join(logs.output))

    def test_missing_revision_warns_and_keeps_node(self):
        self.i2c.read_plan[(ADDR, i2c_node.READ_ADDRESS)] = [[0x01]]
        with mock.patch.object(i2c_node.I2CNode, 'read_block',
                               return_value=None):
            with self.assertLogs('i2c_node', level='WARNING') as logs:
                nodes = i2c_node.discover(0, self.i2c)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].api_level, 0)
        self.assertIn('Unable to read revision code from node 1',
                      '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
