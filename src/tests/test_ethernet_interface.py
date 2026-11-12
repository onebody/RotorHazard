'''EthernetInterface 的准入与会话语义（SPEC §3.4 六门 + §7.1/§7.2 + D-2/D-11）。

只测接口层，不挂 server.py（那是 Phase 2.4）。下行帧一律经注入的 sender 收集，
不上真 socket；真 socket 绑定另有一条冒烟用例。

运行（本文件需要 gevent，用 .venv-rh）：
    cd src/tests && /Users/fcj/workspace/FPV_space/QiYun-FPV-TimingRH/.venv-rh/bin/python -m unittest test_ethernet_interface
'''

import logging
import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
_SRC_DIR = os.path.dirname(_TESTS_DIR)
sys.path.append(os.path.join(_SRC_DIR, 'server'))
sys.path.append(os.path.join(_SRC_DIR, 'interface'))

import gevent.event  # noqa: F401  eventmanager 在类体里用 gevent.event，需先有人导入
import ene_protocol as enp
from EthernetInterface import EthernetInterface
from interface_mapper import InterfaceMapper, InterfaceType

# 坏载荷与失联这两条 warning 是用例故意触发的观测输出，静音后测试日志才可读
logging.getLogger('EthernetInterface').setLevel(logging.CRITICAL)

UID = 'LPTC5-001'
UID2 = 'LPTC5-002'
NODE_ADDR = ('192.0.2.10', 9022)
BOOT = 0x11223344


class Harness(unittest.TestCase):
    '''公共夹具：一个 8 座位接口、一个假时钟、一个收集下行的 sender。'''

    num_nodes = 8
    whitelist = (UID,)
    seat_plan = None

    def setUp(self):
        self.now_ms = 0
        self.sent = []
        self.interface = EthernetInterface(
            num_nodes=self.num_nodes, whitelist=self.whitelist, seat_plan=self.seat_plan,
            server_session_id=0xFEEDFACE, clock=lambda: self.now_ms, sender=self.sent.append)

    def tick(self, ms):
        self.now_ms += ms

    def send(self, frame, addr=NODE_ADDR):
        self.interface.handle_packet(frame.encode(), addr)

    def announce_frame(self, uid=UID, channel_count=8, boot_id=BOOT, caps=enp.V1_CAPS):
        payload = enp.encode_announce(enp.Announcement(
            node_uid=uid, device_type='QiYun-LPTC5', firmware_version='0.0-test',
            channel_count=channel_count, caps=caps, rssi_unit=enp.RSSI_CODE))
        return enp.Frame(msg_type=enp.MSG.NODE_ANNOUNCE, payload=payload,
                         sequence=1, boot_id=boot_id, uptime_ms=1_000)

    def register_frame(self, uid=UID, channel_count=8, boot_id=BOOT, caps=enp.V1_CAPS,
                       src_port=9022):
        payload = (enp.encode_announce(enp.Announcement(
            node_uid=uid, device_type='QiYun-LPTC5', firmware_version='0.0-test',
            channel_count=channel_count, caps=caps, rssi_unit=enp.RSSI_CODE)) +
            b'\x02\x00\x00\x00\x00\x01' + b'\xc0\x00\x02\x0a' +
            enp.encode_config([(0, src_port)])[1:] + bytes([enp.BOOT_POWER_ON]))
        return enp.Frame(msg_type=enp.MSG.NODE_REGISTER, payload=payload,
                         sequence=2, boot_id=boot_id, uptime_ms=1_100)

    def rssi_frame(self, readings, handle=1, boot_id=BOOT):
        return enp.Frame(msg_type=enp.MSG.RSSI_UPDATE,
                         payload=enp.encode_rssi_update(readings),
                         sequence=4, node_id=handle, boot_id=boot_id,
                         uptime_ms=self.now_ms, channel=0)

    def lap_frame(self, event_id=7, handle=1, boot_id=BOOT, channel=3, uptime_ms=5_000,
                  msg_type=enp.MSG.LAP_EVENT, flags=enp.FLAG_ACK_REQUIRED):
        payload = enp.encode_lap_event(pass_start_uptime_ms=uptime_ms - 20, duration_ms=40,
                                       rssi_peak=210, rssi_threshold=150, signal_quality=0,
                                       event_kind=enp.CROSSING, event_id=event_id)
        return enp.Frame(msg_type=msg_type, payload=payload, sequence=9, node_id=handle,
                         boot_id=boot_id, uptime_ms=uptime_ms, channel=channel, flags=flags)

    def downlink(self):
        '''收集到的下行帧，按到达顺序解码。'''
        return [enp.decode(raw) for _addr, raw in self.sent]

    def last_downlink(self):
        addr, raw = self.sent[-1]
        return addr, enp.decode(raw)


class NodeTable(Harness):
    '''E-2/D-3：网络节点仍然要填 api_level 并自调 init()。'''

    def test_nodes_declare_api_level_18_so_max_rssi_is_255(self):
        for node in self.interface.nodes:
            self.assertEqual(18, node.api_level)
            self.assertEqual(255, node.max_rssi_value)
            self.assertTrue(node.api_valid_flag)
        self.assertEqual(8, len(self.interface.nodes))

    def test_set_frequency_does_not_run_a_register_test(self):
        '''E-3：下发是协议命令；返回 False 会让 RH 报"检查 RX SPI"的假告警。'''
        self.assertTrue(self.interface.set_frequency(0, 5645, 0, 0))
        self.assertEqual(5645, self.interface.nodes[0].frequency)


class ConfigSetters(Harness):
    '''E-1/E-3：RH 的 setter 只改本侧座位状态；配置走 TCP 9023（§7.6/P-4），不得混上 UDP 下行。'''

    def test_a_config_setter_never_touches_the_udp_downlink(self):
        '''把配置"顺手"发到 9022 是最容易犯的错：节点侧收不到，UDP 平面还多了一份野流量。'''
        self.send(self.register_frame())
        self.sent[:] = []
        self.interface.set_frequency(2, 5732, 5, 3)
        self.interface.set_enter_at_level(2, 137)
        self.interface.set_exit_at_level(2, 96)
        self.assertEqual([], self.sent)

    def test_the_setters_land_on_the_seat_rh_reads_back(self):
        self.interface.set_frequency(2, 5732, 5, 3)  # 末两个参数 = RH 的频段/频点序号
        self.interface.set_enter_at_level(2, 137)
        self.interface.set_exit_at_level(2, 96)
        node = self.interface.nodes[2]
        self.assertEqual([5732, 137, 96], [node.frequency, node.enter_at_level, node.exit_at_level])

    def test_a_disabled_seat_keeps_rh_semantics(self):
        '''RH 会用 frequency=0 表示"该座位停用"，这条状态必须留在本侧。'''
        self.interface.set_frequency(0, 0, 0, 0)
        self.assertEqual(0, self.interface.nodes[0].frequency)

    def test_force_end_crossing_has_no_v1_message_and_stays_quiet(self):
        '''§5 无此消息：只能静默，不得伪造一条配置。'''
        self.send(self.register_frame())
        self.sent[:] = []
        self.assertFalse(self.interface.force_end_crossing(0))
        self.assertEqual([], self.sent)


class DiscoveryAndAdmission(Harness):

    def test_announce_only_registers_a_candidate(self):
        '''D-2：发现 ≠ 接入。不建会话、不改座位、不回包。'''
        self.send(self.announce_frame())
        self.assertIn(UID, self.interface.candidates)
        self.assertEqual({}, self.interface.sessions)
        self.assertEqual([], self.sent)

    def test_register_opens_a_session_and_acks_with_a_handle(self):
        self.send(self.register_frame())
        addr, ack = self.last_downlink()
        self.assertEqual(NODE_ADDR, addr)
        self.assertEqual(enp.MSG.NODE_REGISTER_ACK, ack.msg_type)
        body = enp.decode_register_ack(ack.payload)
        self.assertEqual(1, body.assigned_node_id)
        self.assertEqual(enp.E_OK, body.status)
        self.assertEqual(0xFEEDFACE, body.server_session_id)
        self.assertEqual(1_000, body.heartbeat_interval_ms)
        self.assertEqual(enp.V1_CAPS, body.caps_granted & enp.V1_CAPS)

    def test_a_registered_eight_channel_node_occupies_seats_zero_to_seven(self):
        self.send(self.register_frame())
        session = self.interface.sessions[1]
        self.assertEqual(UID, session.node_uid)
        self.assertEqual(0, session.first_seat)
        self.assertEqual(8, session.channel_count)

    def test_a_second_register_is_idempotent_and_keeps_the_seat(self):
        '''§7.2：节点重试 REGISTER（同 boot_id）不得改 seat、不得多发句柄。'''
        self.send(self.register_frame())
        self.send(self.register_frame())
        first, second = self.downlink()
        self.assertEqual(enp.E_OK, enp.decode_register_ack(first.payload).status)
        ack = enp.decode_register_ack(second.payload)
        self.assertEqual(enp.E_DUPLICATE_REG, ack.status)
        self.assertEqual(1, ack.assigned_node_id)  # 回的是当前有效句柄，节点据此收敛
        self.assertEqual(0, self.interface.sessions[1].first_seat)

    def test_a_new_boot_generation_gets_a_new_handle_but_keeps_the_seat(self):
        self.send(self.register_frame())
        self.send(self.register_frame(boot_id=BOOT + 1))
        handles = sorted(self.interface.sessions)
        self.assertEqual([1, 2], handles)
        self.assertEqual(enp.E_OK, enp.decode_register_ack(self.downlink()[-1].payload).status)
        self.assertEqual([0, 0], [self.interface.sessions[h].first_seat for h in handles])

    def test_an_unlisted_uid_is_dropped_and_counted(self):
        self.send(self.register_frame(uid='LPTC5-999'))
        self.assertEqual({}, self.interface.sessions)
        self.assertEqual([], self.sent)
        self.assertEqual(1, self.interface.stats['unauth_drops'])

    def test_a_superseded_generation_stops_accepting_frames(self):
        '''ID-2/§6.4：旧句柄随代际作废（不回收），它的帧不再进计时面。'''
        self.send(self.register_frame())
        self.send(self.register_frame(boot_id=BOOT + 1))
        self.sent[:] = []
        self.send(self.lap_frame(handle=1))
        self.assertEqual([], self.sent)
        self.assertEqual([], self.interface.accepted_events)
        self.assertEqual(1, self.interface.stats['unauth_drops'])


class SeatAllocation(Harness):
    '''C-1 + C-2 的分工：接口只保证「不重叠、不移动」，按稳定 ID 排序在上游 node_mapping。'''

    whitelist = None  # 未配白名单 = 全放行（单机/开发默认，2.4 从配置接入）

    def test_two_nodes_take_non_overlapping_blocks_in_arrival_order(self):
        self.send(self.register_frame(uid=UID2, channel_count=4, boot_id=BOOT + 1))
        self.send(self.register_frame(uid=UID, channel_count=4))
        self.assertEqual([(UID, 4), (UID2, 0)],
                         sorted((s.node_uid, s.first_seat)
                                for s in self.interface.sessions.values()))
        self.assertEqual([1, 2], sorted(self.interface.sessions))


class PreplannedSeats(Harness):
    whitelist = None
    seat_plan = {UID2: 4, UID: 0}

    def test_a_preplanned_seat_map_beats_registration_order(self):
        '''C-2：注册表按 uid 预排的表说了算，谁先上线都不改座位。'''
        self.send(self.register_frame(uid=UID2, channel_count=4, boot_id=BOOT + 1))
        self.send(self.register_frame(uid=UID, channel_count=4))
        self.assertEqual([(UID, 0), (UID2, 4)],
                         sorted((s.node_uid, s.first_seat)
                                for s in self.interface.sessions.values()))


class CollidingPlan(Harness):
    whitelist = None
    seat_plan = {UID2: 0}

    def test_a_plan_that_collides_with_an_occupied_seat_is_refused_not_applied(self):
        '''C-1 优先于 C-2：宁缺不抢，已上线节点的座位绝不能被后来的预排表挪走。'''
        self.send(self.register_frame(uid=UID, channel_count=8))
        self.send(self.register_frame(uid=UID2, channel_count=2, boot_id=BOOT + 1))
        self.assertEqual(enp.E_BAD_VALUE,
                         enp.decode_register_ack(self.downlink()[-1].payload).status)
        self.assertEqual([UID], [s.node_uid for s in self.interface.sessions.values()])


class SeatExhaustion(Harness):
    '''C-1：座位只追加。装不下的节点要明拒，不得挤掉已在用的块。'''

    whitelist = None  # 未配白名单 = 全放行（单机/开发默认，2.4 从配置接入）

    def test_a_node_that_does_not_fit_the_remaining_seats_is_refused(self):
        self.send(self.register_frame(uid='LPTC5-001', channel_count=4))
        self.send(self.register_frame(uid='LPTC5-002', channel_count=4, boot_id=BOOT + 1))
        self.send(self.register_frame(uid='LPTC5-003', channel_count=4, boot_id=BOOT + 2))
        ack = enp.decode_register_ack(self.downlink()[-1].payload)
        self.assertEqual(0, ack.assigned_node_id)
        self.assertEqual(enp.E_BAD_VALUE, ack.status)
        self.assertEqual([1, 2], sorted(self.interface.sessions))
        self.assertEqual([('LPTC5-001', 0), ('LPTC5-002', 4)],
                         sorted((s.node_uid, s.first_seat)
                                for s in self.interface.sessions.values()))


class SixGateMatrix(Harness):
    '''§3.4 的顺序与计数：magic 静默、其余计数并可回 NODE_ERROR。'''

    def test_foreign_magic_is_silent_and_not_counted(self):
        '''§9 规则 1：异构广播不得污染指标。'''
        raw = b'QYEN' + self.register_frame().encode()[4:]
        self.interface.handle_packet(raw, NODE_ADDR)
        self.assertEqual([], self.sent)
        self.assertEqual(0, self.interface.stats['unauth_drops'])

    def test_a_checksum_error_on_a_live_session_asks_for_the_error_code(self):
        self.send(self.register_frame())
        self.sent[:] = []
        raw = bytearray(self.lap_frame().encode())
        raw[30] ^= 0x01
        self.interface.handle_packet(bytes(raw), NODE_ADDR)
        addr, frame = self.last_downlink()
        self.assertEqual(NODE_ADDR, addr)
        self.assertEqual(enp.MSG.NODE_ERROR, frame.msg_type)
        error = enp.decode_error(frame.payload)
        self.assertEqual(enp.E_CHECKSUM, error.error_code)
        self.assertEqual(enp.MSG.LAP_EVENT, error.related_msg_type)
        self.assertEqual(1, self.interface.stats['crc_errors'])

    def test_a_wrong_major_version_is_rejected_not_guessed(self):
        frame = self.register_frame()
        frame.proto_version = 0x0200
        self.send(frame)
        _addr, reply = self.last_downlink()
        self.assertEqual(enp.MSG.NODE_ERROR, reply.msg_type)
        self.assertEqual(enp.E_VERSION, enp.decode_error(reply.payload).error_code)
        self.assertEqual(1, self.interface.stats['version_rejects'])

    def test_an_unassigned_handle_is_dropped_without_breaking_the_session(self):
        self.send(self.register_frame())
        self.sent[:] = []
        self.send(self.lap_frame(handle=99))
        self.assertEqual([], self.sent)
        self.assertEqual(1, self.interface.stats['unauth_drops'])
        self.assertIn(1, self.interface.sessions)

    def test_an_unknown_message_type_acks_an_error_and_keeps_the_session(self):
        '''§9 规则 4：未知 msg_type 不断会话。'''
        self.send(self.register_frame())
        self.sent[:] = []
        self.send(enp.Frame(msg_type=0x0FFF, payload=b'', sequence=3, node_id=1, boot_id=BOOT))
        _addr, reply = self.last_downlink()
        self.assertEqual(enp.MSG.NODE_ERROR, reply.msg_type)
        self.assertEqual(enp.E_UNKNOWN_MSG, enp.decode_error(reply.payload).error_code)
        self.assertIn(1, self.interface.sessions)

    def test_a_malformed_registration_payload_is_counted_not_fatal(self):
        '''载荷坏在门 5 之后：不得让收包绿野因此死掉（任务书 §11 健壮性）。'''
        frame = self.register_frame()
        frame.payload = frame.payload[:12]  # 截断到 announcement 的字符串中间
        self.send(frame)
        self.assertEqual({}, self.interface.sessions)
        self.assertEqual([], self.sent)
        self.assertEqual(1, self.interface.stats['bad_length'])

    def test_a_bad_length_is_counted_as_bad_length(self):
        self.interface.handle_packet(self.register_frame().encode()[:-4], NODE_ADDR)
        self.assertEqual(1, self.interface.stats['bad_length'])


class UplinkData(Harness):

    def test_rssi_update_lands_on_the_matching_seat(self):
        self.send(self.register_frame())
        readings = {channel: 40 + channel for channel in range(1, 9)}
        self.send(self.rssi_frame(readings, handle=1))
        self.assertEqual([41, 42, 43, 44, 45, 46, 47, 48],
                         [node.current_rssi for node in self.interface.nodes])

    def test_a_no_reading_sentinel_does_not_look_like_zero(self):
        '''§2.6：0xFF 是"无有效读数"，既不是 0 也不是峰值。'''
        self.send(self.register_frame())
        self.send(self.rssi_frame({1: 120, 2: 255}, handle=1))
        self.assertEqual(120, self.interface.nodes[0].current_rssi)
        self.assertEqual(0, self.interface.nodes[1].current_rssi)
        self.assertEqual(1, self.interface.stats['rssi_no_reading'])

    def test_heartbeat_marks_the_session_alive(self):
        self.send(self.register_frame())
        self.tick(5_000)
        self.assertTrue(self.interface.sessions[1].offline)
        self.send(enp.Frame(msg_type=enp.MSG.NODE_HEARTBEAT, node_id=1, boot_id=BOOT,
                            uptime_ms=self.now_ms))
        self.assertFalse(self.interface.sessions[1].offline)
        self.tick(3_001)
        self.assertTrue(self.interface.sessions[1].offline)
        self.assertEqual(1, self.interface.stats['offline_sessions'])

    def test_an_offline_node_changes_nothing_about_the_topology(self):
        '''D-2 附带裁定：赛中掉线只告警，不碰座位、不摘接口。'''
        self.send(self.register_frame())
        seats_before = self.interface.sessions[1].first_seat
        self.tick(10_000)
        self.interface.update()
        self.assertEqual(seats_before, self.interface.sessions[1].first_seat)
        self.assertEqual(8, len(self.interface.nodes))


class EventAcceptance(Harness):
    '''2.2 只做到"受理 + ACK + 入队"；投递给计时引擎属 2.3。'''

    def test_a_lap_event_is_acked_with_the_same_identity(self):
        self.send(self.register_frame())
        self.sent[:] = []
        self.send(self.lap_frame(event_id=7, channel=3))
        addr, ack = self.last_downlink()
        self.assertEqual(enp.MSG.LAP_EVENT_ACK, ack.msg_type)
        self.assertEqual(1, ack.node_id)
        self.assertEqual(3, ack.channel)
        body = enp.decode_lap_ack(ack.payload)
        self.assertEqual(7, body.event_id)
        self.assertEqual(enp.E_OK, body.status)
        self.assertEqual(0, body.ch_mask_acked)
        self.assertEqual(NODE_ADDR, addr)
        self.assertEqual(1, len(self.interface.accepted_events))

    def test_a_retransmit_is_acked_the_same_way(self):
        '''§6.1：补传与首发同键，ACK 语义不因 msg_type 改变。'''
        self.send(self.register_frame())
        self.sent[:] = []
        self.send(self.lap_frame(event_id=7, msg_type=enp.MSG.LAP_EVENT_RETRANSMIT,
                                 flags=enp.FLAG_RETRANSMIT))
        self.assertEqual(enp.MSG.LAP_EVENT_ACK, self.last_downlink()[1].msg_type)
        self.assertEqual(1, len(self.interface.accepted_events))

    def test_an_event_outside_the_session_block_is_counted_not_dropped_silently(self):
        '''§2.3：通道号越出该节点的块 ⇒ 计一条，不得静默（任务书 §10-13）。'''
        self.send(self.register_frame(channel_count=2))
        self.sent[:] = []
        self.send(self.lap_frame(channel=5))
        self.assertEqual([], self.sent)
        self.assertEqual([], self.interface.accepted_events)
        self.assertEqual(1, self.interface.stats['bad_channel'])

    def test_an_event_on_an_unknown_boot_generation_is_not_acked(self):
        self.send(self.register_frame())
        self.sent[:] = []
        self.send(self.lap_frame(boot_id=BOOT + 5))
        self.assertEqual([], self.sent)
        self.assertEqual([], self.interface.accepted_events)
        self.assertEqual(1, self.interface.stats['unauth_drops'])


class TimeSyncGating(Harness):

    def test_a_sync_request_never_starts_mid_race(self):
        '''§6.3：赛中刷新映射会把"对偏移免疫"的主成绩拖下水。'''
        self.send(self.register_frame())
        self.sent[:] = []
        self.interface.set_race_status(1)  # RACE_STATUS_RACING
        self.send(enp.Frame(msg_type=enp.MSG.TIME_SYNC_RESPONSE,
                            payload=enp.encode_time_sync_response(1_000, 1_004, 0),
                            node_id=1, boot_id=BOOT, uptime_ms=1_002))
        _addr, reply = self.last_downlink()
        self.assertEqual(enp.MSG.NODE_ERROR, reply.msg_type)
        self.assertEqual(enp.E_SYNC_IN_RACE, enp.decode_error(reply.payload).error_code)
        self.assertEqual({}, self.interface.sessions[1].samples)


class TransportBinding(unittest.TestCase):
    '''启动期真 bind 试探（D-8 附带裁定 ③：不得只比对清单）。'''

    def test_start_binds_and_stop_releases_the_uplink_socket(self):
        interface = EthernetInterface(num_nodes=1, discovery_port=0, uplink_port=0,
                                      sender=lambda item: None)
        interface.start()
        try:
            self.assertTrue(interface.uplink_socket.getsockname()[1] > 0)
        finally:
            interface.stop()
        self.assertIsNone(interface.uplink_socket)


class DefaultDeny(Harness):
    '''D-14：空白名单 = 全拒绝；只有 None 才是夹具/单机开发的全放行语义。'''

    whitelist = ()

    def test_an_empty_whitelist_refuses_registration_without_a_handle(self):
        self.send(self.register_frame())
        self.assertEqual({}, self.interface.sessions)
        self.assertEqual([], self.sent)
        self.assertEqual(1, self.interface.stats['unauth_drops'])

    def test_none_still_means_admit_all_for_fixtures(self):
        interface = EthernetInterface(num_nodes=8, whitelist=None,
                                      clock=lambda: 0, sender=lambda item: None)
        interface.handle_packet(self.register_frame().encode(), NODE_ADDR)
        self.assertEqual([UID], [s.node_uid for s in interface.sessions.values()])


class MapperContract(Harness):
    '''interface_mapper 有四个方法对**所有**接口无条件转发（`:179/:225/:235/:241`），
    ETHNODE 少一个就会在服务器运行期 AttributeError。'''

    def setUp(self):
        super().setUp()
        self.mapper = InterfaceMapper(None)
        self.mapper.add_interface(self.interface, InterfaceType.ETHNODE)

    def test_the_mapper_can_walk_every_interface_for_the_fwupd_port(self):
        self.mapper.close_fwupd_serial_port()

    def test_the_mapper_can_ask_every_interface_to_jump_to_bootloader(self):
        self.mapper.jump_to_bootloader()

    def test_the_mapper_can_set_the_error_report_limit(self):
        self.mapper.set_intf_error_report_percent_limit(1.0)

    def test_a_bad_frame_shows_up_in_the_mapper_error_total(self):
        self.send(self.register_frame())
        raw = bytearray(self.lap_frame().encode())
        raw[30] ^= 0x01
        self.interface.handle_packet(bytes(raw), NODE_ADDR)
        self.assertEqual(1, self.mapper.get_intf_total_error_count())


if __name__ == '__main__':
    unittest.main()
