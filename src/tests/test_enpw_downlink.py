'''ENP-W 下行消息的字节布局（SPEC §7 偏移表手抄，期望值不来自实现）。

这些帧由服务器发出（REGISTER_ACK / LAP_EVENT_ACK / TIME_SYNC_* / CONFIG_* / REBOOT /
NODE_ERROR），向量文件里没有它们的用例（§11 只钉上行面），所以这里手写 hex 钉住。

运行：cd src/tests && python3 -m unittest test_enpw_downlink
'''

import os
import struct
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(os.path.dirname(_TESTS_DIR), 'interface'))

import ene_protocol


def _hex_of(encode, *args):
    return encode(*args).hex()


class DownlinkPayloads(unittest.TestCase):

    def test_register_ack_is_17_bytes_at_specified_offsets(self):
        ack = ene_protocol.RegisterAck(assigned_node_id=1, status=0,
                                       server_session_id=0xDEADBEEF,
                                       heartbeat_interval_ms=1000, rssi_interval_ms=100,
                                       caps_granted=511)
        self.assertEqual('00000001' '00' 'deadbeef' '03e8' '0064' '000001ff',
                         _hex_of(ene_protocol.encode_register_ack, ack))
        self.assertEqual(ack, ene_protocol.decode_register_ack(
            ene_protocol.encode_register_ack(ack)))

    def test_lap_ack_is_7_bytes_and_reserved_mask_is_zero(self):
        ack = ene_protocol.LapAck(event_id=7, status=0)
        self.assertEqual('00000007' '00' '0000', _hex_of(ene_protocol.encode_lap_ack, ack))

    def test_lap_ack_carries_failure_code_in_status(self):
        ack = ene_protocol.LapAck(event_id=7, status=ene_protocol.E_UNAUTH)
        self.assertEqual('00000007' '05' '0000', _hex_of(ene_protocol.encode_lap_ack, ack))

    def test_time_sync_request_is_index_and_count(self):
        self.assertEqual('0008', _hex_of(ene_protocol.encode_time_sync_request, 0, 8))

    def test_time_sync_result_offset_is_signed_32_bit(self):
        result = ene_protocol.TimeSyncResult(offset_ms=-4000, delay_dispersion_ms=8,
                                            samples_used=8, status=0)
        self.assertEqual('fffff060' '0008' '08' '00',
                         _hex_of(ene_protocol.encode_time_sync_result, result))
        self.assertEqual(result, ene_protocol.decode_time_sync_result(
            ene_protocol.encode_time_sync_result(result)))

    def test_config_set_is_count_then_three_bytes_per_item(self):
        items = [(ene_protocol.CFG_FREQ_MHZ, 5645), (ene_protocol.CFG_ENTER_AT_LEVEL, 90)]
        self.assertEqual('02' '01160d' '02005a', _hex_of(ene_protocol.encode_config, items))
        self.assertEqual(items, ene_protocol.decode_config(
            ene_protocol.encode_config(items)))

    def test_config_rejects_more_than_eight_items(self):
        with self.assertRaises(ValueError):
            ene_protocol.encode_config([(0x01, 0)] * 9)

    def test_reboot_is_token_then_delay(self):
        reboot = ene_protocol.Reboot(confirm_token=0x11223344, delay_ms=500)
        self.assertEqual('11223344' '01f4', _hex_of(ene_protocol.encode_reboot, reboot))

    def test_node_error_is_code_related_detail(self):
        error = ene_protocol.NodeError(error_code=ene_protocol.E_CHECKSUM,
                                       related_msg_type=ene_protocol.MSG.LAP_EVENT)
        self.assertEqual('0001' '0303' '00000000', _hex_of(ene_protocol.encode_error, error))

    def test_rssi_update_centidb_uses_int16_min_as_no_reading(self):
        payload = ene_protocol.encode_rssi_update({1: None, 2: -5000},
                                                  ene_protocol.RSSI_CENTIDB)
        self.assertEqual('0003' '8000' 'ec78', payload.hex())
        self.assertEqual({1: None, 2: -5000},
                         ene_protocol.decode_rssi_update(payload,
                                                          ene_protocol.RSSI_CENTIDB).readings)


class DownlinkFrames(unittest.TestCase):

    def test_ack_frame_is_39_bytes_and_echoes_node_and_channel(self):
        frame = ene_protocol.Frame(msg_type=ene_protocol.MSG.LAP_EVENT_ACK,
                                   payload=ene_protocol.encode_lap_ack(
                                       ene_protocol.LapAck(event_id=7, status=0)),
                                   sequence=42, node_id=1, channel=3, uptime_ms=12345)
        raw = frame.encode()
        self.assertEqual(39, len(raw))
        self.assertEqual('5248454e' '0100' '0020' '0304' '0007', raw[:12].hex())
        self.assertEqual((1, 0, 12345, 3), (frame.node_id, frame.boot_id,
                                            frame.uptime_ms, frame.channel))
        decoded = ene_protocol.decode(raw)
        self.assertEqual(1, decoded.node_id)
        self.assertEqual(3, decoded.channel)
        self.assertEqual(0, decoded.boot_id)  # §3.2: 服务器侧消息固定填 0

    def test_zero_length_payload_is_allowed_for_heartbeat(self):
        raw = ene_protocol.Frame(msg_type=ene_protocol.MSG.NODE_HEARTBEAT,
                                 node_id=1).encode()
        self.assertEqual(32, len(raw))
        self.assertEqual(b'', ene_protocol.decode(raw).payload)


class GateOrder(unittest.TestCase):
    '''§3.4 的六道门是有序的，这里钉住容易写反的三条。'''

    def test_length_gate_runs_before_magic(self):
        with self.assertRaises(ene_protocol.ProtocolError) as caught:
            ene_protocol.decode(b'QYEN' + bytes(20))
        self.assertEqual(ene_protocol.E_BAD_LENGTH, caught.exception.code)

    def test_foreign_magic_drops_even_when_checksum_is_broken(self):
        raw = bytearray(ene_protocol.Frame(msg_type=ene_protocol.MSG.NODE_ERROR,
                                           payload=b'\x00\x01').encode())
        raw[0:4] = b'QYEN'
        raw[30] ^= 0xFF
        self.assertIsNone(ene_protocol.decode(bytes(raw)))

    def test_oversized_payload_len_is_bad_length(self):
        raw = ene_protocol.Frame(msg_type=ene_protocol.MSG.NODE_LOG,
                                 payload=bytes(ene_protocol.MAX_PAYLOAD + 1)).encode()
        with self.assertRaises(ene_protocol.ProtocolError) as caught:
            ene_protocol.decode(raw)
        self.assertEqual(ene_protocol.E_BAD_LENGTH, caught.exception.code)

    def test_unknown_flag_bits_do_not_drop_the_frame(self):
        raw = ene_protocol.Frame(msg_type=ene_protocol.MSG.NODE_HEARTBEAT, node_id=1,
                                 flags=1 << 7).encode()
        self.assertEqual(1 << 7, ene_protocol.decode(raw).flags)


class TcpFraming(unittest.TestCase):

    def test_two_frames_and_a_half_frame_in_one_read(self):
        one = ene_protocol.Frame(msg_type=ene_protocol.MSG.NODE_CONFIG_ACK,
                                 payload=ene_protocol.encode_config_ack(
                                     ene_protocol.ConfigAck(status=0, items=[]))).encode()
        two = ene_protocol.Frame(msg_type=ene_protocol.MSG.NODE_LOG,
                                 payload=ene_protocol.encode_log(3, 'lap 7 queued')).encode()
        frames, rest = ene_protocol.split_frames(one + two + two[:10])
        self.assertEqual([ene_protocol.MSG.NODE_CONFIG_ACK, ene_protocol.MSG.NODE_LOG],
                         [frame.msg_type for frame in frames])
        self.assertEqual(two[:10], rest)

    def test_a_header_claiming_an_impossible_payload_len_stops_the_scan(self):
        raw = bytearray(ene_protocol.Frame(msg_type=ene_protocol.MSG.NODE_LOG,
                                           payload=b'x').encode())
        struct.pack_into('>H', raw, 10, ene_protocol.MAX_PAYLOAD + 1)
        self.assertEqual([], ene_protocol.split_frames(bytes(raw))[0])


if __name__ == '__main__':
    unittest.main()
