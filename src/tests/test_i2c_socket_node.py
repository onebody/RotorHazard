'''i2cSocket_node：TCP socket 传输层的单元测试（不真开端口）。

I2CNode 用 socket 承载 RH 的 command/checksum 协议，所以测试全部替换
socket.socket 为 FakeSocket：断言发出去的字节、收读循环、重试与断链语义。

运行：cd src/tests && python -m unittest test_i2c_socket_node -v
（该套件 import RHInterface，需要带 gevent 的解释器，如项目 .venv-rh）
'''

import os
import socket
import sys
import unittest
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)
sys.path.append(os.path.join(_SRC_DIR, 'server'))
sys.path.append(os.path.join(_SRC_DIR, 'interface'))

import i2cSocket_node
from i2cSocket_node import I2CNode


class FakeSocket:
    '''替身：记录 sendall，按队列吐 recv 数据，可指定前 N 次发送失败。'''

    def __init__(self, recv_queue=None, send_failures=0):
        self.recv_queue = list(recv_queue or [])
        self.send_failures = send_failures
        self.sent = []
        self.connected_to = None
        self.closed = False

    def connect(self, address):
        self.connected_to = address

    def sendall(self, data):
        if self.send_failures:
            self.send_failures -= 1
            raise socket.error('send failed')
        self.sent.append(bytes(data))

    def recv(self, size):
        if not self.recv_queue:
            return b''
        return self.recv_queue.pop(0)

    def close(self):
        self.closed = True


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSocket()
        patcher = mock.patch.object(i2cSocket_node.socket, 'socket',
                                    return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.node = I2CNode(0, 8, '127.0.0.1', 5555)

    def block(self, payload, checksum=None):
        payload = bytearray(payload)
        if checksum is None:
            checksum = i2cSocket_node.calculate_checksum(payload)
        payload.append(checksum)
        return bytes(payload)


class DiscoveryTests(unittest.TestCase):
    '''扫描器约定：discover(*args, **kwargs) 返回节点列表。'''

    def test_discover_returns_empty_list(self):
        self.assertEqual(i2cSocket_node.discover(), [])

    def test_discover_tolerates_scanner_kwargs(self):
        self.assertEqual(i2cSocket_node.discover(config={}, idxOffset=3,
                                                 isS32BPillFlag=False), [])


class ConnectionTests(Base):
    def test_connect_opens_inet_stream_to_configured_address(self):
        self.node.connect()
        self.assertEqual(self.fake.connected_to, ('127.0.0.1', 5555))

    def test_disconnect_closes_and_clears_socket(self):
        self.node.socket = self.fake
        self.node.disconnect()
        self.assertTrue(self.fake.closed)
        self.assertIsNone(self.node.socket)

    def test_disconnect_without_socket_is_noop(self):
        self.node.disconnect()
        self.assertIsNone(self.node.socket)

    def test_send_command_connects_lazily(self):
        self.node.send_command(0x03)
        self.assertEqual(self.fake.connected_to, ('127.0.0.1', 5555))

    def test_receive_response_connects_lazily(self):
        self.fake.recv_queue = [b'\x01\x02']
        self.node.receive_response(2)
        self.assertEqual(self.fake.connected_to, ('127.0.0.1', 5555))


class SendCommandTests(Base):
    def test_command_only_sends_packed_command(self):
        self.node.socket = self.fake
        self.node.send_command(0x03)
        self.assertEqual(self.fake.sent,
                         [bytes(i2cSocket_node.pack_16(0x03))])

    def test_command_with_payload_is_appended(self):
        self.node.socket = self.fake
        self.node.send_command(0x51, b'\x01\x02')
        self.assertEqual(self.fake.sent,
                         [bytes(i2cSocket_node.pack_16(0x51)) + b'\x01\x02'])

    def test_send_error_disconnects_and_reraises(self):
        self.fake.send_failures = 1
        self.node.socket = self.fake
        with self.assertRaises(socket.error):
            self.node.send_command(0x03)
        self.assertIsNone(self.node.socket)
        self.assertTrue(self.fake.closed)


class ReceiveResponseTests(Base):
    def test_accumulates_chunks_until_size(self):
        self.fake.recv_queue = [b'\x01', b'\x02\x03']
        self.node.socket = self.fake
        self.assertEqual(bytes(self.node.receive_response(3)), b'\x01\x02\x03')

    def test_closed_connection_raises_and_disconnects(self):
        self.node.socket = self.fake
        with self.assertRaises(socket.error):
            self.node.receive_response(2)
        self.assertIsNone(self.node.socket)

    def test_recv_error_disconnects_and_reraises(self):
        class Broken(FakeSocket):
            def recv(self, size):
                raise socket.error('boom')

        self.node.socket = Broken()
        with self.assertRaises(socket.error):
            self.node.receive_response(1)
        self.assertIsNone(self.node.socket)


class ReadBlockTests(Base):
    def test_valid_block_returns_payload_without_checksum(self):
        self.fake.recv_queue = [self.block(b'\x07\x08')]
        self.node.socket = self.fake
        self.assertEqual(bytes(self.node.read_block(0x03, 2)), b'\x07\x08')
        self.assertEqual(self.node.read_block_count, 1)
        self.assertEqual(self.node.read_error_count, 0)

    def test_bad_checksum_retries_then_succeeds(self):
        self.fake.recv_queue = [self.block(b'\x07\x08', checksum=0xFF),
                                self.block(b'\x07\x08')]
        self.node.socket = self.fake
        self.assertEqual(bytes(self.node.read_block(0x03, 2, max_retries=2)),
                         b'\x07\x08')
        self.assertEqual(self.node.read_error_count, 1)
        self.assertEqual(len(self.fake.sent), 2)

    def test_checksum_never_valid_returns_none(self):
        bad = self.block(b'\x07\x08', checksum=0xFF)
        self.fake.recv_queue = [bad, bad]
        self.node.socket = self.fake
        self.assertIsNone(self.node.read_block(0x03, 2, max_retries=1))
        self.assertEqual(self.node.read_error_count, 2)

    def test_socket_error_exhausts_retries_and_returns_none(self):
        self.node.socket = self.fake
        self.node.send_command = mock.Mock(side_effect=socket.error('down'))
        self.assertIsNone(self.node.read_block(0x03, 2, max_retries=1))
        self.assertEqual(self.node.send_command.call_count, 2)
        self.assertEqual(self.node.read_error_count, 2)


class WriteBlockTests(Base):
    def test_payload_appended_with_command_below_api_20(self):
        self.node.socket = self.fake
        self.node.api_level = 19
        self.assertTrue(self.node.write_block(0x51, bytearray(b'\x01\x02')))
        expected = bytearray(b'\x01\x02\x51')
        expected.append(i2cSocket_node.calculate_checksum(expected))
        self.assertEqual(self.fake.sent,
                         [bytes(i2cSocket_node.pack_16(0x51))
                          + bytes(expected)])

    def test_payload_appended_without_command_from_api_20(self):
        self.node.socket = self.fake
        self.node.api_level = 20
        self.assertTrue(self.node.write_block(0x51, bytearray(b'\x01\x02')))
        expected = bytearray(b'\x01\x02')
        expected.append(i2cSocket_node.calculate_checksum(expected))
        self.assertEqual(self.fake.sent,
                         [bytes(i2cSocket_node.pack_16(0x51))
                          + bytes(expected)])

    def test_caller_data_is_not_mutated(self):
        self.node.socket = self.fake
        data = bytearray(b'\x01\x02')
        self.node.write_block(0x51, data)
        self.assertEqual(bytes(data), b'\x01\x02')

    def test_send_error_retries_then_succeeds(self):
        self.fake.send_failures = 1
        self.node.socket = self.fake
        self.assertTrue(self.node.write_block(0x51, bytearray(b'\x01')))
        self.assertEqual(self.node.write_error_count, 1)
        self.assertEqual(len(self.fake.sent), 1)

    def test_all_retries_fail_returns_false(self):
        self.fake.send_failures = 99
        self.node.socket = self.fake
        self.assertFalse(self.node.write_block(0x51, bytearray(b'\x01')))
        self.assertEqual(self.node.write_error_count,
                         i2cSocket_node.MAX_RETRY_COUNT + 1)


class HigherLevelTests(Base):
    def test_read_firmware_version_decodes_and_strips_nulls(self):
        self.node.read_block = mock.Mock(return_value=bytearray(b'4.6.0\0\0'))
        self.node.read_firmware_version()
        self.assertEqual(self.node.firmware_version_str, '4.6.0')

    def test_read_firmware_proctype(self):
        self.node.read_block = mock.Mock(return_value=bytearray(b'ESP32\0'))
        self.node.read_firmware_proctype()
        self.assertEqual(self.node.firmware_proctype_str, 'ESP32')

    def test_read_firmware_timestamp_joins_date_and_time(self):
        self.node.read_block = mock.Mock(
            side_effect=[bytearray(b'Nov 16 2026\0'), bytearray(b'16:37:00\0')])
        self.node.read_firmware_timestamp()
        self.assertEqual(self.node.firmware_timestamp_str,
                         'Nov 16 2026 16:37:00')

    def test_read_firmware_timestamp_none_when_date_missing(self):
        self.node.read_block = mock.Mock(return_value=None)
        self.node.read_firmware_timestamp()
        self.assertIsNone(self.node.firmware_timestamp_str)
        self.assertEqual(self.node.read_block.call_count, 1)

    def test_jump_to_bootloader_sends_0x01(self):
        self.node.socket = self.fake
        self.node.jump_to_bootloader()
        self.assertEqual(self.fake.sent,
                         [bytes(i2cSocket_node.pack_16(0x01))])

    def test_send_status_message_packs_type_and_data(self):
        self.node.api_level = 35
        self.node.write_block = mock.Mock()
        self.node.send_status_message(2, 7)
        self.node.write_block.assert_called_once_with(
            i2cSocket_node.SEND_STATUS_MESSAGE,
            i2cSocket_node.pack_16((2 << 8) | 7))

    def test_send_status_message_ignored_below_api_35(self):
        self.node.api_level = 34
        self.node.write_block = mock.Mock()
        self.node.send_status_message(2, 7)
        self.node.write_block.assert_not_called()


if __name__ == '__main__':
    unittest.main()
