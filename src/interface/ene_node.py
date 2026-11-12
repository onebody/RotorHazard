'''ENP-W 的会话与座位块对象（SPEC §6.4 / §7.2，裁定 D-2 / D-11）。

这里只放状态与纯判定；收帧、六门、ACK 都在 EthernetInterface。
'''

import collections

# 入账队列的元素：一条 = 一个已通过 §6.1 去重键的事件，等待 §6.3 的映射落轴后投递。
AcceptedEvent = collections.namedtuple(
    'AcceptedEvent', 'handle seat uid boot_id event_id uptime_ms event')


class SeatBlock:
    '''一个 uid 占用的连续座位。生命周期长于会话：C-1 只追加、C-4 摘除留空位。'''

    def __init__(self, node_uid, first_seat, channel_count):
        self.node_uid = node_uid
        self.first_seat = first_seat
        self.channel_count = channel_count

    def seat_of(self, channel):
        '''通道号（1 基，SPEC §2.3）→ 座位下标；越界返回 None。'''
        if 1 <= channel <= self.channel_count:
            return self.first_seat + channel - 1
        return None


class Session:
    '''一次 REGISTER 建立的活动会话；`handle` 就是帧头的 node_id（ID-2）。'''

    OFFLINE_AFTER_BEATS = 3  # SPEC §8.1：连续 3 个心跳周期无包 = 失联

    def __init__(self, handle, block, peer, boot_id, rssi_unit, caps_granted,
                 heartbeat_interval_ms, clock):
        self.handle = handle
        self.block = block
        self.peer = peer
        self.boot_id = boot_id
        self.rssi_unit = rssi_unit
        self.caps_granted = caps_granted
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self._clock = clock
        self.last_seen_ms = int(clock())
        self.samples = {}  # §6.3 四时间戳样本 {index: (t1, t2, t3, t4)}
        self.offset_ms = None
        self.delay_ms = None
        self.samples_used = 0
        self.sync_pending = {}  # §7.8 未配对请求 {sample_index: T1}
        self.sync_started_ms = None
        self.max_uptime_ms = -1  # §6.4 同代际内已见过的最新事件时间
        self.event_keys = set()  # §6.1 去重键 (channel, event_id)，代际私有
        self.clock_suspect = False
        self.superseded = False
        self._offline_alarmed = False

    @property
    def calibrated(self):
        '''没有 offset 就无从把 uptime 落到比赛时轴上（§6.3）。'''
        return self.offset_ms is not None

    @property
    def node_uid(self):
        return self.block.node_uid

    @property
    def first_seat(self):
        return self.block.first_seat

    @property
    def channel_count(self):
        return self.block.channel_count

    @property
    def offline(self):
        return (int(self._clock()) - self.last_seen_ms) > \
            self.OFFLINE_AFTER_BEATS * self.heartbeat_interval_ms

    def beat(self):
        '''任何来自本会话的合法帧都算一次心跳；恢复后重新允许一次失联告警。'''
        self.last_seen_ms = int(self._clock())
        self._offline_alarmed = False

    def notice_offline(self):
        '''失联只在转换沿告警一次（收包循环 10 ms 一轮，不得刷屏）。'''
        if self.offline and not self._offline_alarmed:
            self._offline_alarmed = True
            return True
        return False


def discover(*args, **kwargs):
    # RH 节点插件扫描器按 *_node 后缀 import 本模块并调 discover()（见 interface/Plugins.py）。
    # ETHNODE 由 server.py 经 mapper 手动挂载、不走插件发现，故返回空以免被记为加载错误。
    return []
