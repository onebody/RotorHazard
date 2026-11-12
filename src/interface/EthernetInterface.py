'''RotorHazard 以太网分布式计时节点接口（ENP-W 1.0）。

规范正文只有一份：docs/ETHERNET_PROTOCOL_SPEC.md。线格式在 ene_protocol.py（标准库实现，
与 tools/ethernet_node_simulator 那份互为交叉验证，唯一契约是 SPEC §11 黄金向量）。

本文件覆盖 Phase 2.2 + 2.3：UDP 绑定 + SPEC §3.4 六门 + D-2 发现/准入 + 会话与座位 +
RSSI 落座 + 事件去重与 ACK + §6.3 时钟校准 + §6.2 落轴投递（只走 pass_record_callback）。
仍未落地（按阶段归属）：
  · server.py 挂载、TCP 9023 控制面（§7.6 的 CONFIG_*、P-4）：Phase 2.4/2.5
'''

import logging
import os
import select
import socket
import statistics
import struct
from time import monotonic

import gevent

import ene_node
import ene_protocol as enp
from BaseHardwareInterface import BaseHardwareInterface
from Node import Node

logger = logging.getLogger(__name__)

UPDATE_SLEEP = float(os.environ.get('RH_ENE_POLL_INTERVAL', '0.01'))
MAX_DATAGRAM = enp.HEADER_LEN + enp.MAX_PAYLOAD
API_LEVEL = 18                    # E-2：网络节点自报，不靠 interface_mapper 补
HEARTBEAT_INTERVAL_MS = 1000      # SPEC §7.2 默认
RSSI_INTERVAL_MS = 0              # 0 = 不要求周期上报：V1 走事件驱动（方式 B）
SYNC_SAMPLES = 8                  # SPEC §7.8：一批建议 8 样本
SYNC_DISPERSION_LIMIT_MS = 50     # 与模拟器 ServerConfig.sync_dispersion_limit_ms 同值
SYNC_BATCH_TIMEOUT_MS = 1_000     # 一批凑不齐的兜底：不得永久锁死校准
ROLLBACK_THRESHOLD_MS = 5_000     # SPEC §6.4：同代际 uptime 倒退阈值，与模拟器同值

# 门 3/4 的失败 → §8.1 的计数名
_STATS_FOR_ERROR = {
    enp.E_BAD_LENGTH: 'bad_length',
    enp.E_CHECKSUM: 'crc_errors',
    enp.E_VERSION: 'version_rejects',
}

# 2.2 只作存活观测、尚未解读载荷的消息（beat() 已刷新 last_seen）
_OBSERVED_ONLY = frozenset((enp.MSG.NODE_HEARTBEAT, enp.MSG.NODE_STATUS,
                            enp.MSG.NODE_LOG, enp.MSG.RF_STATUS))


def _peek_header(data):
    '''从坏帧里尽力抠出 (msg_type, node_id)：长度不够就回 0，用于定址错误回执。'''
    if len(data) < enp.HEADER_LEN:
        return 0, 0
    return (struct.unpack_from('>H', data, 8)[0], struct.unpack_from('>I', data, 16)[0])


class EthernetInterface(BaseHardwareInterface):
    '''8（或 N）个座位 = N 路网络计时节点；一个节点可占连续多座。'''

    def __init__(self, *args, **kwargs):
        BaseHardwareInterface.__init__(self)
        self.config = kwargs.get('config')
        self.num_nodes = int(kwargs.get('num_nodes', 8))
        self.clock = kwargs.get('clock') or (lambda: int(1000 * monotonic()))
        self._sender = kwargs.get('sender')
        self.whitelist = kwargs.get('whitelist')
        self.seat_plan = kwargs.get('seat_plan') or {}
        self.server_session_id = kwargs.get('server_session_id', 0)
        self.discovery_port = kwargs.get('discovery_port', enp.DISCOVERY_PORT)
        self.uplink_port = kwargs.get('uplink_port', enp.UPLINK_PORT)

        self.nodes = []
        for index in range(self.num_nodes):
            node = Node()
            node.index = index
            node.api_level = API_LEVEL
            node.init()  # → api_valid_flag=True、max_rssi_value=255（D-3/E-1）
            self.nodes.append(node)

        self.candidates = {}      # uid → Announcement：D-2「发现 ≠ 接入」
        self.sessions = {}        # 句柄 → Session（含已作废代际，供观测）
        self.accepted_events = []  # Phase 2.3 的入口队列
        self.uplink_socket = None
        self._discovery_socket = None
        self.update_thread = None
        self._by_uid = {}
        self._blocks = {}
        self._used_seats = set()
        self._next_free_seat = 0
        self._next_handle = 1     # 句柄单调分配、永不回收（ID-2）
        self._tx_sequence = 0
        self._stats = dict.fromkeys(('bad_length', 'crc_errors', 'version_rejects',
                                     'unauth_drops', 'unknown_msg', 'rssi_no_reading',
                                     'bad_channel', 'duplicate_events', 'clock_suspects'), 0)

    #
    # 生命周期
    #

    @property
    def ready(self):
        '''D-8 附带裁定 ③：能不能用只看真 bind 的结果，不看配置清单。'''
        return self.uplink_socket is not None

    def start(self):
        if self.update_thread is not None:
            return
        self.uplink_socket = self._bind_udp(self.uplink_port, 'uplink')
        if self.uplink_socket is None:
            return  # 上行都收不到，这个接口就没有意义，交给 server.py 报就绪失败
        self._discovery_socket = self._bind_udp(self.discovery_port, 'discovery')
        self.log('ENP-W uplink on UDP {}, discovery on UDP {}'.format(
            self.uplink_socket.getsockname()[1],
            self._discovery_socket.getsockname()[1] if self._discovery_socket else 'disabled'))
        self.update_thread = gevent.spawn(self.update_loop)

    def stop(self):
        if self.update_thread:
            self.update_thread.kill(block=True, timeout=0.5)
        self.update_thread = None
        for name in ('_discovery_socket', 'uplink_socket'):
            sock = getattr(self, name)
            if sock:
                sock.close()
            setattr(self, name, None)

    def _bind_udp(self, port, name):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('', port))
        except OSError as error:
            sock.close()
            self.ready_failure_msg = 'ENP-W {} UDP/{}: {}'.format(name, port, error)
            logger.warning(self.ready_failure_msg)
            return None
        return sock

    def update_loop(self):
        while True:
            sockets = [sock for sock in (self._discovery_socket, self.uplink_socket) if sock]
            for sock in select.select(sockets, [], [], UPDATE_SLEEP)[0]:
                self._receive(sock)
            self.update()

    def _receive(self, sock):
        try:
            data, addr = sock.recvfrom(MAX_DATAGRAM)
        except OSError as error:  # 单个数据报的错误不得终止收包循环
            logger.debug('ENP-W recvfrom failed: %s', error)
            return
        self.handle_packet(data, addr)

    def update(self):
        '''周期动作：失联告警 → 时钟校准 → 落轴投递。失联只告警、不改拓扑（D-2 附带裁定）。'''
        now_ms = int(self.clock())
        for session in self.sessions.values():
            if session.notice_offline():
                logger.warning('ENP-W node {} ({}) lost heartbeat'.format(
                    session.handle, session.node_uid))
            if session.superseded:
                continue  # 作废代际不再校准（§6.4/ID-2）
            if session.sync_pending and \
                    now_ms - session.sync_started_ms >= SYNC_BATCH_TIMEOUT_MS:
                # 一批里有节点没答的样本：到时限按已到样本收口，不能让静默锁死校准
                self._finish_sync(session)
            if self.race_status == self.RACE_STATUS_READY and not session.calibrated and \
                    not session.sync_pending and \
                    (session.sync_started_ms is None or
                     now_ms - session.sync_started_ms >= SYNC_BATCH_TIMEOUT_MS):
                self._start_sync(session, now_ms)
        self._deliver()

    #
    # 收帧：SPEC §3.4 六道门，顺序即优先级
    #

    @property
    def stats(self):
        '''§8.1 网络质量观测面。

        两个 gauge 不是累计：`offline_sessions` = 当前失联会话数，
        `unsynced_events` = 因会话还没拿到 offset 而扣在队列里的事件数（§6.3）。
        '''
        return dict(self._stats,
                    offline_sessions=sum(1 for session in self.sessions.values()
                                         if session.offline),
                    unsynced_events=sum(
                        1 for accepted in self.accepted_events
                        if not (self.sessions.get(accepted.handle)
                                 and self.sessions[accepted.handle].calibrated)))

    def handle_packet(self, data, addr):
        try:
            frame = enp.decode(data)
        except enp.ProtocolError as error:
            self._on_bad_frame(data, addr, error)
            return
        if frame is None:  # 门 2：异构广播 ⇒ 静默、不计数（§9 规则 1）
            return
        try:
            self._on_frame(frame, addr)
        except (ValueError, IndexError, struct.error) as error:
            self._stats['bad_length'] += 1  # 门 5 之后的坏载荷：计数并继续收包
            logger.warning('ENP-W msg_type=0x%04X payload rejected: %s',
                           frame.msg_type, error)

    def _on_bad_frame(self, data, addr, error):
        self._stats[_STATS_FOR_ERROR[error.code]] += 1
        if error.code == enp.E_BAD_LENGTH:
            return  # 连头部都不可信，无法定址 ⇒ 只计数
        msg_type, handle = _peek_header(data)
        session = self.sessions.get(handle)
        if error.code == enp.E_VERSION or session:  # 门 3 只给已注册会话回错（§3.4）
            self._send(enp.MSG.NODE_ERROR,
                       enp.encode_error(enp.NodeError(error.code, msg_type)),
                       addr=addr, node_id=handle if session else 0)

    def _on_frame(self, frame, addr):
        if frame.msg_type == enp.MSG.NODE_ANNOUNCE:
            announce = enp.decode_announce(frame.payload)
            self.candidates[announce.node_uid] = announce  # 只登记候选，不建会话、不回包（D-2）
            return
        if frame.msg_type == enp.MSG.NODE_REGISTER:
            self._admit(enp.decode_register(frame.payload), frame, addr)
            return
        session = self._authorize(frame)
        if session:
            session.beat()
            self._dispatch(frame, session)

    def _authorize(self, frame):
        '''门 5：句柄已注册、代际相符、未被换代作废。'''
        session = self.sessions.get(frame.node_id)
        if session is None or session.superseded or session.boot_id != frame.boot_id:
            self._stats['unauth_drops'] += 1
            return None
        return session

    def _dispatch(self, frame, session):
        msg_type = frame.msg_type
        if msg_type == enp.MSG.RSSI_UPDATE:
            self._on_rssi(frame, session)
        elif msg_type in (enp.MSG.LAP_EVENT, enp.MSG.LAP_EVENT_RETRANSMIT):
            self._on_lap_event(frame, session)
        elif msg_type == enp.MSG.TIME_SYNC_RESPONSE:
            self._on_time_sync(frame, session)
        elif msg_type not in _OBSERVED_ONLY:
            self._stats['unknown_msg'] += 1  # 门 6：回错误但不断会话（§9 规则 4）
            self._send_error(enp.E_UNKNOWN_MSG, msg_type, session)

    #
    # 发现与准入（D-2）
    #

    def _admit(self, registration, frame, addr):
        announce = registration.announce
        uid = announce.node_uid
        self.candidates[uid] = announce
        if self.whitelist is not None and uid not in self.whitelist:
            # D-14：空元组 = 全拒绝；只有 None 才是夹具/单机开发的全放行语义
            self._stats['unauth_drops'] += 1
            return
        current = self._by_uid.get(uid)
        if current and current.boot_id == frame.boot_id:
            current.peer = addr
            # §7.2 幂等：重试 REGISTER 保留旧句柄与旧 seat，回 E_DUPLICATE_REG 让节点收敛
            self._send_register_ack(current.handle, enp.E_DUPLICATE_REG, addr,
                                    current.caps_granted)
            return
        if current:
            current.superseded = True  # §6.4 换代：旧句柄作废不回收（ID-2），seat 不变（C-4）
        block = self._blocks.get(uid) or self._allocate(announce)
        if block is None:
            self._send_register_ack(0, enp.E_BAD_VALUE, addr, 0)  # C-1：装不下就明拒
            return
        session = ene_node.Session(
            handle=self._next_handle, block=block, peer=addr, boot_id=frame.boot_id,
            rssi_unit=announce.rssi_unit, caps_granted=announce.caps & enp.V1_CAPS,
            heartbeat_interval_ms=HEARTBEAT_INTERVAL_MS, clock=self.clock)
        self._next_handle += 1
        self.sessions[session.handle] = session
        self._by_uid[uid] = session
        self.log('Registered node {} ({} channels) at seat {}'.format(
            uid, block.channel_count, block.first_seat + 1))
        self._send_register_ack(session.handle, enp.E_OK, addr, session.caps_granted)

    def _allocate(self, announce):
        '''C-1：座位不重叠、不移动；C-2 的按稳定 ID 排序由上游预排后从 `seat_plan` 传入。'''
        count = announce.channel_count
        first = self.seat_plan.get(announce.node_uid, self._next_free_seat)
        seats = range(first, first + count)
        if not 1 <= count <= enp.MAX_CHANNELS or first < 0 or \
                first + count > len(self.nodes) or \
                any(seat in self._used_seats for seat in seats):
            return None  # 装不下或与已占座位相撞 ⇒ 明拒，绝不挪走已在用的块
        block = ene_node.SeatBlock(announce.node_uid, first, count)
        self._blocks[announce.node_uid] = block
        self._used_seats.update(seats)
        self._next_free_seat = max(self._next_free_seat, first + count)
        return block

    def _send_register_ack(self, handle, status, addr, caps_granted):
        body = enp.RegisterAck(assigned_node_id=handle, status=status,
                               server_session_id=self.server_session_id,
                               heartbeat_interval_ms=HEARTBEAT_INTERVAL_MS,
                               rssi_interval_ms=RSSI_INTERVAL_MS, caps_granted=caps_granted)
        self._send(enp.MSG.NODE_REGISTER_ACK, enp.encode_register_ack(body), addr=addr)

    #
    # 上行数据面
    #

    def _on_rssi(self, frame, session):
        update = enp.decode_rssi_update(frame.payload, session.rssi_unit)
        for channel, value in update.readings.items():
            seat = session.block.seat_of(channel)
            if seat is None:
                self._stats['bad_channel'] += 1
            elif value is None:
                # §2.6：无读数既不是 0 也不是峰值 ⇒ 保持上一次的值，只计数
                self._stats['rssi_no_reading'] += 1
            else:
                self.nodes[seat].current_rssi = value

    def _observe_rollback(self, session, frame):
        '''§6.4：同代际内 uptime 大幅倒退 ⇒ 标 CLOCK_SUSPECT；补传形态不参与（§6.4 末段）。'''
        if frame.msg_type == enp.MSG.LAP_EVENT_RETRANSMIT:
            return
        if frame.uptime_ms + ROLLBACK_THRESHOLD_MS < session.max_uptime_ms:
            session.clock_suspect = True
            self._stats['clock_suspects'] += 1
            logger.warning('ENP-W node {} uptime went backwards {}ms (clock suspect)'
                           .format(session.node_uid, session.max_uptime_ms - frame.uptime_ms))
        session.max_uptime_ms = max(session.max_uptime_ms, frame.uptime_ms)

    def _on_lap_event(self, frame, session):
        seat = session.block.seat_of(frame.channel)
        if seat is None:
            self._stats['bad_channel'] += 1
            return
        self._observe_rollback(session, frame)
        event = enp.decode_lap_event(frame.payload)
        key = (frame.channel, event.event_id)
        if key in session.event_keys:
            # §6.1：同一事件只进计时系统一次；重复帧仍要 ACK，否则节点会无限重传
            self._stats['duplicate_events'] += 1
        else:
            session.event_keys.add(key)
            self.accepted_events.append(ene_node.AcceptedEvent(
                handle=session.handle, seat=seat, uid=session.node_uid,
                boot_id=session.boot_id, event_id=event.event_id,
                uptime_ms=frame.uptime_ms, event=event))
        # §7.5：ACK 回填被确认帧的 node_id/channel；补传与首发同语义（§6.1）
        self._send(enp.MSG.LAP_EVENT_ACK,
                   enp.encode_lap_ack(enp.LapAck(event_id=event.event_id, status=enp.E_OK)),
                   addr=session.peer, node_id=session.handle, channel=frame.channel)

    def _on_time_sync(self, frame, session):
        if self.race_status != self.RACE_STATUS_READY:
            # T-2：场内 offset 是常量，赛中刷新映射会把对偏移免疫的主成绩拖下水
            self._send_error(enp.E_SYNC_IN_RACE, frame.msg_type, session)
            return
        response = enp.decode_time_sync_response(frame.payload)
        index = response.sample_index
        if index not in session.sync_pending:
            return  # 迟到的重复响应：没有对应的请求就没人能用它
        t1 = session.sync_pending.pop(index)
        if t1 != response.echo_t1:
            return  # §7.8：错配样本绝不参与偏移计算
        session.samples[index] = (t1, frame.uptime_ms, response.t3_uptime_ms,
                                  int(self.clock()))
        if not session.sync_pending:
            self._finish_sync(session)  # 全部请求都有了下文 ⇒ 可以结算（含被丢弃的错配样本）

    #
    # 时钟校准（SPEC §6.3 / §7.8）
    #

    def _start_sync(self, session, now_ms):
        session.samples = {}
        session.sync_started_ms = now_ms
        session.sync_pending = {}
        for index in range(SYNC_SAMPLES):
            frame = self._send(enp.MSG.TIME_SYNC_REQUEST,
                               enp.encode_time_sync_request(index, SYNC_SAMPLES),
                               addr=session.peer, node_id=session.handle)
            session.sync_pending[index] = frame.uptime_ms

    def _finish_sync(self, session):
        '''一批样本 → 偏移中位数 + 离散度告警；一枚都没配到则报「节点不可达」等下一批。'''
        session.sync_pending = {}  # 收口即结清：未答的请求不再占着这一批
        pairs = [enp.sync_offset_and_delay(*stamp) for stamp in session.samples.values()]
        status = enp.SYNC_NODE_UNREACHABLE
        offset = delay = dispersion = 0
        if pairs:
            offsets = [pair[0] for pair in pairs]
            delays = [pair[1] for pair in pairs]
            dispersion = max(delays) - min(delays)
            offset = int(statistics.median(offsets))
            delay = int(statistics.median(delays))
            status = (enp.SYNC_OK if dispersion <= SYNC_DISPERSION_LIMIT_MS
                      else enp.SYNC_SCATTER_TOO_HIGH)
            session.offset_ms, session.delay_ms, session.samples_used = \
                offset, delay, len(offsets)
            if status != enp.SYNC_OK:
                session.clock_suspect = True
        self._send(enp.MSG.TIME_SYNC_RESULT,
                   enp.encode_time_sync_result(enp.TimeSyncResult(
                       offset_ms=offset, delay_dispersion_ms=dispersion,
                       samples_used=len(pairs), status=status)),
                   addr=session.peer, node_id=session.handle)
        if status == enp.SYNC_NODE_UNREACHABLE:
            logger.warning('ENP-W node {} answered no sync sample'.format(session.node_uid))

    #
    # 落轴投递（SPEC §6.2 / §7.5 / §8 S-1）
    #

    def _deliver(self):
        '''已校准会话的事件按校正时间升序交给 RH；未校准的原样留在队列里等。

        权威事件时间 = 帧头 `uptime_ms`（§7.5 峰值中点），映射 `服务器 ms = uptime − offset`
        与接口时轴的 `1000*monotonic()` 同刻度，因此 `add_lap` 拿到的就是它期望的绝对秒。
        '''
        if not callable(self.pass_record_callback):
            return  # Phase 2.4 挂载前回调还是 None：事件留在队列，不得凭空消失
        hold, deliver = [], []
        for accepted in self.accepted_events:
            session = self.sessions.get(accepted.handle)
            if session is None or not session.calibrated:
                hold.append(accepted)  # 扣住不是丢弃
            else:
                deliver.append(((accepted.uptime_ms - session.offset_ms) / 1000.0, accepted))
        # §6.2：排序键只取校正后的事件时间；同值时不得去比 dataclass 里的载荷
        for absolute_seconds, accepted in sorted(deliver, key=lambda item: item[0]):
            self.pass_record_callback(self.nodes[accepted.seat], absolute_seconds,
                                      self.LAP_SOURCE_REALTIME,
                                      peak=accepted.event.rssi_peak)
        self.accepted_events = hold

    #
    # 下行
    #

    def _send_error(self, code, related_msg_type, session):
        self._send(enp.MSG.NODE_ERROR,
                   enp.encode_error(enp.NodeError(code, related_msg_type)),
                   addr=session.peer, node_id=session.handle)

    def _send(self, msg_type, payload, addr, node_id=0, channel=0):
        self._tx_sequence = (self._tx_sequence + 1) & 0xFFFF
        frame = enp.Frame(msg_type=msg_type, payload=payload, sequence=self._tx_sequence,
                          node_id=node_id, channel=channel, uptime_ms=int(self.clock()))
        # D-11：服务器侧帧的 boot_id 恒 0 —— 它没有「上电代际」
        raw = frame.encode()
        if self._sender:  # 测试/审计夹具注入的出口：接管传输，避免双发
            self._sender((addr, raw))
        elif self.uplink_socket:
            self.uplink_socket.sendto(raw, addr)
        return frame  # §7.8 的 T1 就是这里的 uptime_ms，调用方要把它记下来配对

    #
    # RH 侧 setter（interface_mapper 的契约面，E-1）
    #

    def set_frequency(self, node_index, frequency, *_args):
        # E-3：本侧只记状态。真下发是 CONFIG_SET，走 TCP 9023（§7.6/P-4，Phase 2.5）。
        # 返回值不得是 False —— 那会让 RH 弹出「检查 RX SPI」的假告警。
        self.nodes[node_index].frequency = frequency
        return True

    def set_enter_at_level(self, node_index, level):
        self.nodes[node_index].enter_at_level = level

    def transmit_enter_at_level(self, node, level):
        return level

    def set_exit_at_level(self, node_index, level):
        self.nodes[node_index].exit_at_level = level

    def transmit_exit_at_level(self, node, level):
        return level

    def force_end_crossing(self, node_index):
        return False  # §5 无对应消息：只能静默不支持，不得伪造一条配置

    # interface_mapper 对**所有**接口无条件转发的四个（`:179/:225/:235/:241`）：
    # 网络节点没有串口也没有 bootloader，但缺方法会让服务器运行期 AttributeError。

    def jump_to_bootloader(self):
        self.log('ENP-W nodes have no bootloader path; NODE_REBOOT goes via TCP 9023')

    def close_fwupd_serial_port(self):
        pass

    def get_intf_total_error_count(self):
        return self._stats['bad_length'] + self._stats['crc_errors'] + \
            self._stats['version_rejects']

    def set_intf_error_report_percent_limit(self, percentVal):
        pass  # 错误率报表是 RH 读块语义（E-5），网络侧的质量面走 stats


def get_hardware_interface(*args, **kwargs):
    '''RH 的接口工厂入口（与 MockInterface/RHInterface 同形）。'''
    logger.info('Using ENP-W Ethernet node interface')
    return EthernetInterface(*args, **kwargs)
