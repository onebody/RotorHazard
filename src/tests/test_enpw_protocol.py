'''ENP-W 1.0 线格式的第二实现（核心侧）对 SPEC §11 黄金向量的验收。

向量是唯一契约：期望值是人手抄自 docs/ETHERNET_PROTOCOL_SPEC.md 的字面量，
本文件的观测值只能来自 ene_protocol 的解码，两边都不许自证。

运行方式（与 src/tests 其余套件一致，禁用 discover）：
    cd src/tests && python3 -m unittest test_enpw_protocol
'''

import json
import os
import pathlib
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)

sys.path.append(os.path.join(_SRC_DIR, 'interface'))

import ene_protocol

VECTORS = pathlib.Path(_TESTS_DIR) / 'vectors' / 'enpw_frames.json'

DATA = json.loads(VECTORS.read_text(encoding='utf-8'))
ITEMS = {item['id']: item for item in DATA['vectors']}

_ERRORS = {
    ene_protocol.E_CHECKSUM: 'E_CHECKSUM',
    ene_protocol.E_VERSION: 'E_VERSION',
    ene_protocol.E_BAD_LENGTH: 'E_BAD_LENGTH',
    ene_protocol.E_UNKNOWN_MSG: 'E_UNKNOWN_MSG',
}


def _record(frame, frame_len):
    return {
        'verdict': 'ok',
        'msg_type': '0x{:04X}'.format(frame.msg_type),
        'frame_len': frame_len,
        'payload_len': len(frame.payload),
        'payload': frame.payload.hex(),
        'header_len': frame.header_len,
        'unknown_tail': frame.unknown_tail.hex(),
        'node_id': frame.node_id,
        'boot_id': frame.boot_id,
        'uptime_ms': frame.uptime_ms,
        'channel': frame.channel,
        'flags': frame.flags,
        'sequence': frame.sequence,
    }


def _decoded(raw):
    try:
        frame = ene_protocol.decode(raw)
    except ene_protocol.ProtocolError as error:
        return {'verdict': 'error', 'error': _ERRORS[error.code]}
    return frame


def _observe(item):
    raws = [bytes.fromhex(frame) for frame in item['frames']]
    kind = item['kind']

    if kind == 'sync':
        request = _decoded(raws[0])
        response = _decoded(raws[1])
        echo = ene_protocol.decode_time_sync_response(response.payload)
        offset_ms, delay_ms = ene_protocol.sync_offset_and_delay(
            request.uptime_ms, response.uptime_ms, echo.t3_uptime_ms, item['arrive_ms'][1])
        return {'echo_t1': echo.echo_t1, 'offset_ms': offset_ms, 'delay_ms': delay_ms}

    frame = _decoded(raws[0])
    if isinstance(frame, dict):
        return frame
    if frame is None:
        return {'verdict': 'drop'}

    record = _record(frame, len(raws[0]))
    if kind == 'announce':
        announce = ene_protocol.decode_announce(frame.payload)
        record.update({
            'uid': announce.node_uid,
            'device_type': announce.device_type,
            'firmware_version': announce.firmware_version,
            'channel_count': announce.channel_count,
            'caps': announce.caps,
            'rssi_unit': announce.rssi_unit,
        })
    elif kind == 'lap':
        event = ene_protocol.decode_lap_event(frame.payload)
        record.update({
            'event_id': event.event_id,
            'pass_start_uptime_ms': event.pass_start_uptime_ms,
            'duration_ms': event.duration_ms,
            'rssi_peak': event.rssi_peak,
            'rssi_threshold': event.rssi_threshold,
            'event_kind': event.event_kind,
        })
    elif kind == 'rssi':
        update = ene_protocol.decode_rssi_update(frame.payload, ene_protocol.RSSI_CODE)
        values = [value for value in update.readings.values() if value is not None]
        record.update({
            'ch_mask': update.ch_mask,
            'no_reading': sum(1 for value in update.readings.values() if value is None),
            'best_rssi': max(values) if values else None,
        })
    return record


class VectorContract(unittest.TestCase):
    '''向量文件与 SPEC §11 对得上，且解码器守长度门。'''

    def test_protocol_identity_is_enpw_1_0(self):
        self.assertEqual('ENP-W 1.0', DATA['protocol'])

    def test_every_case_numbered_in_the_spec_is_present_in_order(self):
        self.assertEqual(['V-{}'.format(i) for i in range(1, 16)],
                         [item['id'] for item in DATA['vectors']])

    def test_decoder_rejects_a_frame_shorter_than_the_header(self):
        with self.assertRaises(ene_protocol.ProtocolError) as caught:
            ene_protocol.decode(b'RHEN' + bytes(20))
        self.assertEqual(ene_protocol.E_BAD_LENGTH, caught.exception.code)


class CodecVectors(unittest.TestCase):
    '''逐条向量：解码观测值 == 手写期望值。'''

    def _check(self, vector_id):
        item = ITEMS[vector_id]
        if item['kind'] == 'ingest':
            self.skipTest('{} 属会话/去重面，由 test_ethernet_timing_path.'
                          'GoldenIngestVectors 验收'.format(vector_id))
        observed = _observe(item)
        wrong = {}
        for field, value in item['expect'].items():
            if field == 'payload' and isinstance(value, str) and value.startswith('<ref:'):
                value = _observe(ITEMS[value[5:-1]])['payload']
            if observed.get(field) != value:
                wrong[field] = [value, observed.get(field)]
        self.assertEqual({}, wrong, '{}: {}'.format(vector_id, wrong))

    def test_v1_node_announce(self):
        self._check('V-1')

    def test_v2_rssi_update_eight_channels(self):
        self._check('V-2')

    def test_v3_lap_event_peak_midpoint(self):
        self._check('V-3')

    def test_v4_retransmit_payload_equals_v3(self):
        self._check('V-4')

    def test_v5_checksum_bit_flip(self):
        self._check('V-5')

    def test_v6_foreign_magic_is_dropped_silently(self):
        self._check('V-6')

    def test_v7_major_version_two(self):
        self._check('V-7')

    def test_v8_unknown_header_tail(self):
        self._check('V-8')

    def test_v9_payload_length_mismatch(self):
        self._check('V-9')

    def test_v10_unknown_message_type(self):
        self._check('V-10')

    def test_v11_four_timestamp_pairing(self):
        self._check('V-11')

    def test_v12_repeated_delivery_counts_duplicates(self):
        self._check('V-12')

    def test_v13_new_generation_reuses_event_id(self):
        self._check('V-13')

    def test_v14_clock_regression_is_suspect(self):
        self._check('V-14')

    def test_v15_rssi_sentinels_are_not_zero(self):
        self._check('V-15')


if __name__ == '__main__':
    unittest.main()
