'''Phase 2.3：去重、时钟校准与落轴投递（SPEC §6.1 / §6.2 / §6.3 / §6.4 / §7.8）。

夹具复用 test_ethernet_interface 的帧构造器，本文件只加计时面的断言。

运行（需要 gevent，用 .venv-rh）：
    cd src/tests && /Users/fcj/workspace/FPV_space/QiYun-FPV-TimingRH/.venv-rh/bin/python -m unittest test_ethernet_timing_path
'''

import json
import os
import pathlib
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)
sys.path.append(os.path.join(_SRC_DIR, 'server'))
sys.path.append(os.path.join(_SRC_DIR, 'interface'))

import ene_protocol as enp
from BaseHardwareInterface import BaseHardwareInterface
from EthernetInterface import EthernetInterface

from test_enpw_protocol import VECTORS
from test_ethernet_interface import BOOT, UID, Harness

RACING = BaseHardwareInterface.RACE_STATUS_RACING
READY = BaseHardwareInterface.RACE_STATUS_READY
NODE_SKEW = 4000  # 节点 uptime 比服务器 ms 快这么多（§6.3 的 offset 真值基准）
WIRE_DELAY = 6    # 单程取 3 ms，往返 6 ms；故意用偶数，绕开 D-13 未定的奇数取整口径


class TimingHarness(Harness):
    '''注册一个 8 通道节点，并把 §7.8 的四时间戳批跑完（offset 落在 4003）。'''

    def setUp(self):
        super().setUp()
        self.passes = []
        self.interface.pass_record_callback = self.record_pass
        self.send(self.register_frame())
        self.sent[:] = []

    def record_pass(self, node, lap_timestamp_absolute, source, **kwargs):
        self.passes.append((node, lap_timestamp_absolute, source, kwargs))

    def sync_requests(self):
        return [frame for frame in self.downlink()
                if frame.msg_type == enp.MSG.TIME_SYNC_REQUEST]

    def result_frames(self):
        return [frame for frame in self.downlink()
                if frame.msg_type == enp.MSG.TIME_SYNC_RESULT]

    def answer_requests(self, node_skew=NODE_SKEW, wire_delay=WIRE_DELAY):
        '''把这一批请求按 §7.8 全部答回去；T4 = 答完时的假时钟。'''
        requests = self.sync_requests()
        self.sent[:] = []
        self.tick(wire_delay // 2)
        for request in requests:
            sample_index, _count = enp.decode_time_sync_request(request.payload)
            t1 = request.uptime_ms
            self.send(enp.Frame(
                msg_type=enp.MSG.TIME_SYNC_RESPONSE,
                payload=enp.encode_time_sync_response(
                    t1, t1 + node_skew + wire_delay, sample_index),
                node_id=1, boot_id=BOOT, uptime_ms=t1 + node_skew + wire_delay // 2))
        return requests

    def calibrate(self, **kwargs):
        self.interface.update()
        self.answer_requests(**kwargs)
        return self.interface.sessions[1]


class IngestDedup(Harness):
    '''§6.1：去重键 = (uid, boot_id, channel, event_id)；补传与首发同键 ⇒ 天然幂等。'''

    def setUp(self):
        super().setUp()
        self.send(self.register_frame())
        self.sent[:] = []

    def test_the_same_key_twice_enters_the_ingest_path_once(self):
        self.send(self.lap_frame(event_id=7))
        self.send(self.lap_frame(event_id=7))
        self.assertEqual(1, len(self.interface.accepted_events))
        self.assertEqual(1, self.interface.stats['duplicate_events'])

    def test_a_retransmit_of_the_same_event_is_a_duplicate_not_a_new_pass(self):
        self.send(self.lap_frame(event_id=7))
        self.send(self.lap_frame(event_id=7, msg_type=enp.MSG.LAP_EVENT_RETRANSMIT,
                                 flags=enp.FLAG_RETRANSMIT))
        self.assertEqual(1, len(self.interface.accepted_events))
        self.assertEqual(1, self.interface.stats['duplicate_events'])
        # 但补传照样要 ACK，否则节点会无限重传（§6.5）
        self.assertEqual(2, len(self.downlink()))

    def test_sequence_numbers_do_not_participate_in_dedup(self):
        '''§6.1：sequence 是传输层观测量，节点重启后重新取随机起点。'''
        first = self.lap_frame(event_id=7)
        second = self.lap_frame(event_id=7)
        second.sequence = 50_000
        self.send(first)
        self.send(second)
        self.assertEqual(1, len(self.interface.accepted_events))

    def test_the_same_event_id_on_another_channel_is_another_event(self):
        self.send(self.lap_frame(event_id=7, channel=3))
        self.send(self.lap_frame(event_id=7, channel=4))
        self.assertEqual(2, len(self.interface.accepted_events))
        self.assertEqual(0, self.interface.stats['duplicate_events'])

    def test_a_reused_event_id_after_a_new_boot_is_not_a_duplicate(self):
        self.send(self.lap_frame(event_id=7))
        self.send(self.register_frame(boot_id=BOOT + 1))
        self.send(self.lap_frame(event_id=7, handle=2, boot_id=BOOT + 1))
        self.assertEqual(2, len(self.interface.accepted_events))
        self.assertEqual(0, self.interface.stats['duplicate_events'])

    def test_both_frames_of_a_duplicate_get_an_ack(self):
        '''服务器对重复事件幂等：第二次仍然 ACK（节点据此停止重传），但不重复入账。'''
        self.send(self.lap_frame(event_id=7))
        self.send(self.lap_frame(event_id=7))
        kinds = [frame.msg_type for frame in self.downlink()]
        self.assertEqual([enp.MSG.LAP_EVENT_ACK] * 2, kinds)


class ClockSanity(Harness):
    '''§6.4：同代际 uptime 大幅倒退 ⇒ 判时钟异常，但事件照收（标注而非丢弃）。'''

    def setUp(self):
        super().setUp()
        self.send(self.register_frame())
        self.sent[:] = []

    def test_a_backwards_jump_within_a_generation_flags_the_clock(self):
        self.send(self.lap_frame(event_id=7, uptime_ms=60_000))
        self.send(self.lap_frame(event_id=8, uptime_ms=1_000))
        self.assertEqual(1, self.interface.stats['clock_suspects'])
        self.assertEqual(2, len(self.interface.accepted_events))
        self.assertTrue(self.interface.sessions[1].clock_suspect)

    def test_a_retransmit_never_blames_the_clock(self):
        '''§6.4 末段：补传按 event_id 升序回传，uptime 天生更早，参与判定会把每次补传误判成故障。'''
        self.send(self.lap_frame(event_id=7, uptime_ms=60_000))
        self.send(self.lap_frame(event_id=6, uptime_ms=1_000,
                                 msg_type=enp.MSG.LAP_EVENT_RETRANSMIT,
                                 flags=enp.FLAG_RETRANSMIT))
        self.assertEqual(0, self.interface.stats['clock_suspects'])
        self.assertEqual(2, len(self.interface.accepted_events))

    def test_a_small_step_backwards_is_tolerated(self):
        self.send(self.lap_frame(event_id=7, uptime_ms=60_000))
        self.send(self.lap_frame(event_id=8, uptime_ms=59_990))
        self.assertEqual(0, self.interface.stats['clock_suspects'])


class Calibration(TimingHarness):
    '''§6.3 + §7.8：只在 READY 校准、一批 8 样本、错配样本不得参与算偏移。'''

    def test_ready_runs_one_batch_of_eight_requests(self):
        self.interface.update()
        requests = self.sync_requests()
        self.assertEqual(8, len(requests))
        self.assertEqual(list(range(8)),
                         sorted(enp.decode_time_sync_request(f.payload)[0]
                                for f in requests))
        self.assertEqual([8] * 8, [enp.decode_time_sync_request(f.payload)[1]
                                   for f in requests])

    def test_the_offset_and_delay_come_from_the_four_timestamps(self):
        session = self.calibrate()
        self.assertEqual(NODE_SKEW + WIRE_DELAY // 2, session.offset_ms)
        self.assertEqual(0, session.delay_ms)
        self.assertEqual(8, session.samples_used)
        self.assertFalse(session.clock_suspect)

    def test_the_result_is_reported_back_to_the_node(self):
        self.calibrate()
        result = enp.decode_time_sync_result(self.result_frames()[0].payload)
        self.assertEqual(NODE_SKEW + WIRE_DELAY // 2, result.offset_ms)
        self.assertEqual(8, result.samples_used)
        self.assertEqual(enp.SYNC_OK, result.status)

    def test_a_mismatched_echo_t1_drops_that_sample_only(self):
        '''§7.8：echo_t1 与请求不符 ⇒ 只丢这一枚样本，其余照常参与中位数。'''
        self.interface.update()
        requests = self.sync_requests()
        self.sent[:] = []
        self.tick(3)
        for index, request in enumerate(requests):
            t1 = request.uptime_ms - (1 if index == 0 else 0)  # 第一条故意错配
            self.send(enp.Frame(
                msg_type=enp.MSG.TIME_SYNC_RESPONSE,
                payload=enp.encode_time_sync_response(
                    request.uptime_ms + (0 if index else 1),
                    t1 + NODE_SKEW + WIRE_DELAY, index),
                node_id=1, boot_id=BOOT, uptime_ms=t1 + NODE_SKEW + WIRE_DELAY // 2))
        self.assertEqual(7, self.interface.sessions[1].samples_used)

    def test_a_wide_dispersion_marks_the_clock_suspect_and_reports_it(self):
        '''样本够配对但离散度超标：偏移仍然给出，只是标注时钟可疑（§6.3/§7.8）。'''
        self.interface.update()
        for index, request in enumerate(self.sync_requests()):
            t1 = request.uptime_ms
            skew = index * 50  # 样本之间漂 350 ms ⇒ 离散度远超阈值
            self.send(enp.Frame(
                msg_type=enp.MSG.TIME_SYNC_RESPONSE,
                payload=enp.encode_time_sync_response(t1, t1 + NODE_SKEW + skew, index),
                node_id=1, boot_id=BOOT, uptime_ms=t1 + NODE_SKEW + skew // 2))
        session = self.interface.sessions[1]
        self.assertTrue(session.clock_suspect)
        self.assertEqual(enp.SYNC_SCATTER_TOO_HIGH, self.result_status())

    def result_status(self):
        return enp.decode_time_sync_result(self.result_frames()[-1].payload).status

    def test_a_silent_node_is_closed_out_as_unreachable(self):
        '''未凑齐的批次不得永久锁死校准：到时限收口，空批报「节点不可达」并可重来。'''
        self.interface.update()
        self.assertEqual(8, len(self.sync_requests()))
        self.sent[:] = []
        self.tick(999)
        self.interface.update()
        self.assertEqual([], self.sync_requests())  # 未到时限：既不补发请求也不结算
        self.tick(1)
        self.interface.update()
        self.assertEqual(enp.SYNC_NODE_UNREACHABLE, self.result_status())
        self.assertEqual(8, len(self.sync_requests()))
        self.assertIsNone(self.interface.sessions[1].offset_ms)

    def test_no_batch_is_started_while_racing(self):
        '''§6.3：场内映射是常量，赛中刷新会把对偏移免疫的主成绩拖下水。'''
        self.interface.set_race_status(RACING)
        self.interface.update()
        self.assertEqual([], self.sync_requests())
        self.interface.set_race_status(READY)
        self.interface.update()
        self.assertEqual(8, len(self.sync_requests()))  # 回到 READY 才允许校准

    def test_a_new_generation_starts_uncalibrated(self):
        '''§6.4：boot_id 变化 ⇒ offset 与差分基线全部作废重来。'''
        self.calibrate()
        self.send(self.register_frame(boot_id=BOOT + 1))
        self.assertIsNone(self.interface.sessions[2].offset_ms)

    def test_a_calibrated_session_is_not_calibrated_again(self):
        self.calibrate()
        self.sent[:] = []
        self.interface.update()
        self.interface.update()
        self.assertEqual([], self.sync_requests())


class Delivery(TimingHarness):
    '''落轴：服务器 ms = uptime_ms − offset，再除以 1000 交给 pass_record_callback。'''

    def test_an_event_lands_on_the_race_clock_via_the_callback(self):
        '''§7.5：权威事件时间 = 帧头 uptime_ms（峰值中点），不是载荷里的穿越起点。'''
        session = self.calibrate()
        self.send(self.lap_frame(event_id=7, channel=1, uptime_ms=10_000 + session.offset_ms))
        self.interface.update()
        self.assertEqual([(0, 10.0, BaseHardwareInterface.LAP_SOURCE_REALTIME)],
                         [(entry[0].index, entry[1], entry[2]) for entry in self.passes])

    def test_the_peak_of_the_pass_rides_the_same_callback(self):
        '''RH 的 add_lap 用 kwargs['peak'] 记峰值（BaseHardwareInterface:133 同形）。'''
        self.calibrate()
        self.send(self.lap_frame(event_id=7, uptime_ms=20_000 +
                                 self.interface.sessions[1].offset_ms))
        self.interface.update()
        self.assertEqual({'peak': 210}, self.passes[0][3])

    def test_events_are_delivered_in_event_time_not_arrival_order(self):
        '''§6.2：排序依据是校正后的事件时间，不是到达顺序。'''
        offset = self.calibrate().offset_ms
        self.send(self.lap_frame(event_id=8, channel=3, uptime_ms=11_000 + offset))
        self.send(self.lap_frame(event_id=9, channel=3, uptime_ms=10_000 + offset))
        self.interface.update()
        self.assertEqual([10.0, 11.0], [entry[1] for entry in self.passes])

    def test_an_uncalibrated_session_holds_its_events(self):
        '''没有 offset 就无从落轴：投递必须等校准完成，且不得静默丢弃。'''
        self.send(self.lap_frame(event_id=7, uptime_ms=60_000))
        self.interface.update()
        self.assertEqual([], self.passes)
        self.assertEqual(1, self.interface.stats['unsynced_events'])
        self.assertEqual(1, len(self.interface.accepted_events))

    def test_held_events_are_released_once_the_session_gets_an_offset(self):
        '''扣住不是丢弃：校准完成后同一批事件照常补投。'''
        self.send(self.lap_frame(event_id=7, uptime_ms=60_000))
        self.interface.update()
        self.assertEqual([], self.passes)
        session = self.calibrate()
        self.interface.update()
        self.assertEqual(1, len(self.passes))
        self.assertEqual([(60_000 - session.offset_ms) / 1000.0],
                         [entry[1] for entry in self.passes])
        self.assertEqual([], self.interface.accepted_events)

    def test_the_seat_node_is_the_one_rh_will_bind_the_pilot_to(self):
        offset = self.calibrate().offset_ms
        self.send(self.lap_frame(event_id=7, channel=5, uptime_ms=30_000 + offset))
        self.interface.update()
        self.assertEqual(4, self.passes[0][0].index)  # 通道 5 = 座位下标 4
        self.assertIs(self.interface.nodes[4], self.passes[0][0])

    def test_events_are_not_delivered_before_the_callback_is_wired(self):
        '''server.py 的挂载属 Phase 2.4；回调没接上时事件留在队列，不得凭空消失。'''
        self.interface.pass_record_callback = None
        offset = self.calibrate().offset_ms
        self.send(self.lap_frame(event_id=7, uptime_ms=30_000 + offset))
        self.interface.update()
        self.assertEqual([], self.passes)
        self.assertEqual(1, len(self.interface.accepted_events))

    def test_two_channels_stamped_in_the_same_millisecond_both_deliver(self):
        '''同时穿越是常态：时间戳相等时排序不得去比载荷（dataclass 没有序）。'''
        offset = self.calibrate().offset_ms
        self.send(self.lap_frame(event_id=8, channel=4, uptime_ms=12_000 + offset))
        self.send(self.lap_frame(event_id=7, channel=2, uptime_ms=12_000 + offset))
        self.interface.update()
        self.assertEqual(2, len(self.passes))
        self.assertEqual([1, 3], sorted(entry[0].index for entry in self.passes))

    def test_ingested_events_leave_the_queue_once_delivered(self):
        offset = self.calibrate().offset_ms
        self.send(self.lap_frame(event_id=7, uptime_ms=30_000 + offset))
        self.interface.update()
        self.assertEqual([], self.interface.accepted_events)


class GoldenIngestVectors(unittest.TestCase):
    '''SPEC §11 的 ingest 面向量在核心侧的验收（D-12 交叉验证的门禁）。

    `delivered` 在核心侧 = 进入入账队列 `accepted_events` 的条数：§6.1 量的是
    "同一事件只允许进入计时系统一次"，投递排空属下游动作，夹具不调 update()，
    因此与夹具服务器 MockServer.sink 的计数等价。
    '''

    ADDR = ('192.0.2.10', 9022)
    ITEMS = {item['id']: item for item in
             json.loads(pathlib.Path(VECTORS).read_text(encoding='utf-8'))['vectors']}

    def run_vector(self, vector_id):
        now = [0]
        interface = EthernetInterface(num_nodes=8, whitelist=(UID,),
                                      clock=lambda: now[0], sender=lambda frame: None)
        item = self.ITEMS[vector_id]
        for raw, arrive_ms in zip(item['frames'], item['arrive_ms']):
            now[0] = arrive_ms
            interface.handle_packet(bytes.fromhex(raw), self.ADDR)
        observed = {'delivered': len(interface.accepted_events),
                    'duplicate_events': interface.stats['duplicate_events'],
                    'unauth_drops': interface.stats['unauth_drops'],
                    'clock_suspects': interface.stats['clock_suspects']}
        wrong = {field: [want, observed[field]]
                 for field, want in item['expect'].items() if observed[field] != want}
        self.assertEqual({}, wrong, '{}: {}'.format(vector_id, wrong))

    def test_v12_repeated_delivery_counts_duplicates(self):
        self.run_vector('V-12')

    def test_v14_clock_regression_is_suspect(self):
        self.run_vector('V-14')

    def test_v13_new_generation_reuses_event_id(self):
        '''D-15 裁定「改向量、保持规范」：换代先重新 REGISTER 取新句柄，再投第二枚。'''
        self.run_vector('V-13')


if __name__ == '__main__':
    unittest.main()
