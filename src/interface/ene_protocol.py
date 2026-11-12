'''ENP-W 1.0 线格式（RotorHazard Ethernet Node Protocol, wire layer）。

规范正文只有一份：本项目 docs/ETHERNET_PROTOCOL_SPEC.md。本文件是它的**核心侧实现**，
与 tools/ethernet_node_simulator/enpw/ 那份互为交叉验证，两者的唯一契约是 SPEC §11
黄金向量（src/tests/vectors/enpw_frames.json）。改协议必须先改 SPEC，再重生成向量，
两个实现跟着向量走。

只用标准库：本模块不得 import gevent / RH 侧任何模块，固件与测试夹具都要能独立跑。
'''

import socket
import struct
from dataclasses import dataclass, field

MAGIC = b'RHEN'
PROTO_VERSION = 0x0100
HEADER_LEN = 32
MAX_PAYLOAD = 1024
MAX_CHANNELS = 16  # ch_mask 是 u16（SPEC §2.3 / §7.3）

# D-8 裁定的端口预算，改动即视为新的裁定（SPEC §4）
DISCOVERY_PORT = 9020
UPLINK_PORT = 9021
NODE_PORT = 9022
CONTROL_PORT = 9023
MULTICAST_GROUP = '239.194.0.1'
BROADCAST_ADDR = '255.255.255.255'

FLAG_ACK_REQUIRED = 1 << 0
FLAG_RETRANSMIT = 1 << 1

_HEADER = struct.Struct('>4sHHHHIIIIBBH')

# SPEC §8.5 错误码（全部 <= 0xFF），本文件与 ene_protocol 的使用方都只从这里取值
E_CHECKSUM = 0x01
E_VERSION = 0x02
E_BAD_LENGTH = 0x03
E_UNKNOWN_MSG = 0x04
E_UNAUTH = 0x05
E_DUPLICATE_REG = 0x06
E_SYNC_IN_RACE = 0x07
E_UNKNOWN_KEY = 0x08
E_BAD_VALUE = 0x09
E_OK = 0x00

ERROR_NAMES = {
    E_OK: 'E_OK',
    E_CHECKSUM: 'E_CHECKSUM',
    E_VERSION: 'E_VERSION',
    E_BAD_LENGTH: 'E_BAD_LENGTH',
    E_UNKNOWN_MSG: 'E_UNKNOWN_MSG',
    E_UNAUTH: 'E_UNAUTH',
    E_DUPLICATE_REG: 'E_DUPLICATE_REG',
    E_SYNC_IN_RACE: 'E_SYNC_IN_RACE',
    E_UNKNOWN_KEY: 'E_UNKNOWN_KEY',
    E_BAD_VALUE: 'E_BAD_VALUE',
}


class ProtocolError(Exception):
    '''SPEC §3.4 校验矩阵中**会上报**的失败。

    `magic` 不符走静默丢弃（§9 规则 1），返回 None 而不抛本异常。
    '''

    def __init__(self, code, detail=''):
        super().__init__(ERROR_NAMES.get(code, '0x{:02X}'.format(code)) +
                         (' ' + detail if detail else ''))
        self.code = code
        self.detail = detail


class MSG:
    '''SPEC §5 消息号表：高字节 = 平面，低字节 = 序号。'''
    INVALID = 0x0000
    NODE_ANNOUNCE = 0x0101
    NODE_REGISTER = 0x0102
    NODE_REGISTER_ACK = 0x0103
    NODE_HEARTBEAT = 0x0104
    NODE_STATUS = 0x0105
    NODE_REBOOT = 0x0106
    NODE_ERROR = 0x0107
    NODE_LOG = 0x0108
    NODE_CONFIG_GET = 0x0201
    NODE_CONFIG_SET = 0x0202
    NODE_CONFIG_ACK = 0x0203
    RF_STATUS = 0x0301
    RSSI_UPDATE = 0x0302
    LAP_EVENT = 0x0303
    LAP_EVENT_ACK = 0x0304
    LAP_EVENT_RETRANSMIT = 0x0305
    TIME_SYNC_REQUEST = 0x0401
    TIME_SYNC_RESPONSE = 0x0402
    TIME_SYNC_RESULT = 0x0403


def crc16(data):
    '''CRC-16/CCITT-FALSE：poly 0x1021、init 0xFFFF、不反转、xorout 0（SPEC §3.2）。'''
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


@dataclass
class Frame:
    '''固定 32 字节头（SPEC §3.2）。载荷是原样字节，语义由 msg_type 决定。'''
    msg_type: int
    payload: bytes = b''
    sequence: int = 0
    node_id: int = 0
    boot_id: int = 0
    uptime_ms: int = 0
    channel: int = 0
    flags: int = 0
    proto_version: int = PROTO_VERSION
    header_len: int = HEADER_LEN
    unknown_tail: bytes = b''

    def encode(self):
        prefix = _HEADER.pack(
            MAGIC, self.proto_version, self.header_len, self.msg_type, len(self.payload),
            self.sequence, self.node_id, self.boot_id, self.uptime_ms,
            self.channel, self.flags, 0)[:30]
        checksum = crc16(prefix + self.payload)
        extra = self.header_len - HEADER_LEN
        tail = self.unknown_tail[:extra].ljust(extra, b'\x00')
        return prefix + struct.pack('>H', checksum) + tail + self.payload


def decode(raw):
    '''按 §3.4 的检查顺序解码。返回 Frame；magic 不符返回 None；其余失败抛 ProtocolError。'''
    if len(raw) < HEADER_LEN:
        raise ProtocolError(E_BAD_LENGTH, 'frame={}B < {}B'.format(len(raw), HEADER_LEN))
    (magic, proto_version, header_len, msg_type, payload_len, sequence, node_id,
     boot_id, uptime_ms, channel, flags, checksum) = _HEADER.unpack_from(raw)
    if header_len < HEADER_LEN or payload_len > MAX_PAYLOAD or \
            header_len + payload_len != len(raw):
        raise ProtocolError(E_BAD_LENGTH, 'header_len={} payload_len={} frame={}B'.format(
            header_len, payload_len, len(raw)))
    if magic != MAGIC:
        return None
    payload = raw[header_len:header_len + payload_len]
    if crc16(raw[:30] + payload) != checksum:
        raise ProtocolError(E_CHECKSUM, 'msg_type=0x{:04X}'.format(msg_type))
    if proto_version >> 8 != PROTO_VERSION >> 8:
        raise ProtocolError(E_VERSION, 'peer=0x{:04X} local=0x{:04X}'.format(
            proto_version, PROTO_VERSION))
    return Frame(msg_type=msg_type, payload=payload, sequence=sequence, node_id=node_id,
                 boot_id=boot_id, uptime_ms=uptime_ms, channel=channel, flags=flags,
                 proto_version=proto_version, header_len=header_len,
                 unknown_tail=raw[HEADER_LEN:header_len])


def split_frames(buf):
    '''TCP 自定界（SPEC §3.3）：返回（完整帧列表，未凑齐一帧的余数字节）。'''
    frames = []
    offset = 0
    while len(buf) - offset >= HEADER_LEN:
        header_len = struct.unpack_from('>H', buf, offset + 6)[0]
        payload_len = struct.unpack_from('>H', buf, offset + 10)[0]
        need = header_len + payload_len
        if header_len < HEADER_LEN or payload_len > MAX_PAYLOAD or len(buf) - offset < need:
            break
        frame = decode(buf[offset:offset + need])
        offset += need
        if frame is not None:
            frames.append(frame)
    return frames, buf[offset:]


#
# 载荷布局（SPEC §7）。字段偏移只在本节出现一次，别处不得重复偏移。
#

CROSSING = 0
ENTER = 1
EXIT = 2
FORCE_END = 3

_LAP_EVENT = struct.Struct('>IHB BBB I')
_LAP_ACK = struct.Struct('>IBH')
_REGISTER_ACK = struct.Struct('>IBIHHI')
_SYNC_REQUEST = struct.Struct('>BB')
_SYNC_RESPONSE = struct.Struct('>IIB')
_SYNC_RESULT = struct.Struct('>iHBB')
_RF_STATUS = struct.Struct('>HBB')
_NODE_STATUS = struct.Struct('>BHIIIHH')
_CONFIG_ITEM = struct.Struct('>BH')
_ERROR = struct.Struct('>HHI')
_REBOOT = struct.Struct('>IH')

RSSI_CODE = 0
RSSI_CENTIDB = 1
NO_READING = {RSSI_CODE: 0xFF, RSSI_CENTIDB: -32768}
_RSSI_FORMAT = {RSSI_CODE: '>B', RSSI_CENTIDB: '>h'}

BOOT_UNKNOWN = 0
BOOT_POWER_ON = 1
BOOT_WATCHDOG = 2
BOOT_COMMAND = 3
BOOT_CRASH = 4

STATE_IDLE = 0
STATE_ARMED = 1
STATE_RACING = 2
STATE_FAULT = 3

SYNC_OK = 0
SYNC_SCATTER_TOO_HIGH = 1
SYNC_NODE_UNREACHABLE = 2

CFG_FREQ_MHZ = 0x01
CFG_ENTER_AT_LEVEL = 0x02
CFG_EXIT_AT_LEVEL = 0x03
CFG_MODE = 0x04
CFG_CROSS_TIMEOUT_S = 0x05
CFG_HEARTBEAT_MS = 0x06
MODE_RSSI = 0
MODE_EVENT = 1
MAX_CONFIG_ITEMS = 8
MAX_LOG_LEN = 96
MAX_UID_LEN = 32

CAP_RF_DETECTION = 1 << 0
CAP_RSSI_REPORT = 1 << 1
CAP_LAP_EVENT = 1 << 2
CAP_MULTI_CHANNEL = 1 << 3
CAP_TIME_SYNC_V1 = 1 << 4
CAP_CONFIG_VIA_TCP = 1 << 5
CAP_REBOOT_CMD = 1 << 6
CAP_CACHE_RETRANSMIT = 1 << 7
CAP_LOG_STREAM = 1 << 8
CAP_RSSI_QUALITY = 1 << 9
CAP_STAMP_US = 1 << 10  # §10 未来扩展，V1 不得声明
V1_CAPS = (CAP_RF_DETECTION | CAP_RSSI_REPORT | CAP_LAP_EVENT | CAP_MULTI_CHANNEL |
           CAP_TIME_SYNC_V1 | CAP_CONFIG_VIA_TCP | CAP_REBOOT_CMD | CAP_CACHE_RETRANSMIT |
           CAP_LOG_STREAM)


@dataclass
class Announcement:
    node_uid: str
    device_type: str
    firmware_version: str
    channel_count: int
    caps: int
    rssi_unit: int


@dataclass
class Registration:
    announce: Announcement
    mac: bytes
    local_ip: str
    udp_src_port: int
    boot_reason: int


@dataclass
class RegisterAck:
    assigned_node_id: int
    status: int
    server_session_id: int
    heartbeat_interval_ms: int
    rssi_interval_ms: int
    caps_granted: int


@dataclass
class RssiUpdate:
    ch_mask: int
    readings: dict = field(default_factory=dict)  # {channel(1-based): value|None}


@dataclass
class LapEvent:
    pass_start_uptime_ms: int
    duration_ms: int
    rssi_peak: int
    rssi_threshold: int
    signal_quality: int
    event_kind: int
    event_id: int


@dataclass
class LapAck:
    event_id: int
    status: int
    ch_mask_acked: int = 0


@dataclass
class TimeSyncResponse:
    echo_t1: int
    t3_uptime_ms: int
    sample_index: int


@dataclass
class TimeSyncResult:
    offset_ms: int
    delay_dispersion_ms: int
    samples_used: int
    status: int


@dataclass
class RfStatus:
    freq_mhz: int
    locked: int
    rssi_now: int


@dataclass
class NodeStatus:
    state: int
    active_mask: int
    events_buffered: int
    events_retransmitted: int
    buffer_overflows: int
    crc_errors_tx: int
    last_error_code: int


@dataclass
class ConfigAck:
    status: int
    items: list = field(default_factory=list)


@dataclass
class NodeError:
    error_code: int
    related_msg_type: int
    detail: int = 0


@dataclass
class Reboot:
    confirm_token: int
    delay_ms: int = 0


def _put_str(value, max_len=MAX_UID_LEN):
    raw = value.encode('ascii')
    if not 1 <= len(raw) <= max_len:
        raise ValueError('length-prefixed string out of 1..{}: {!r}'.format(max_len, value))
    return bytes([len(raw)]) + raw


def _take_str(payload, offset):
    length = payload[offset]
    return payload[offset + 1:offset + 1 + length].decode('ascii'), offset + 1 + length


def encode_announce(announce):
    return (_put_str(announce.node_uid) + _put_str(announce.device_type, 255) +
            _put_str(announce.firmware_version, 255) +
            struct.pack('>BIH', announce.channel_count, announce.caps, announce.rssi_unit))


def decode_announce(payload):
    uid, offset = _take_str(payload, 0)
    device_type, offset = _take_str(payload, offset)
    firmware, offset = _take_str(payload, offset)
    channel_count, caps, rssi_unit = struct.unpack_from('>BIH', payload, offset)
    return Announcement(node_uid=uid, device_type=device_type, firmware_version=firmware,
                        channel_count=channel_count, caps=caps, rssi_unit=rssi_unit)


def _announce_len(payload):
    offset = 0
    for _ in range(3):
        offset = payload[offset] + offset + 1
    return offset + 7  # channel_count(1) + caps(4) + rssi_unit(2)


def encode_register(registration):
    return (encode_announce(registration.announce) + registration.mac +
            socket.inet_aton(registration.local_ip) +
            struct.pack('>HB', registration.udp_src_port, registration.boot_reason))


def decode_register(payload):
    announce_len = _announce_len(payload)
    tail = payload[announce_len:]
    return Registration(announce=decode_announce(payload[:announce_len]), mac=tail[0:6],
                        local_ip=socket.inet_ntoa(tail[6:10]),
                        udp_src_port=struct.unpack_from('>H', tail, 10)[0],
                        boot_reason=tail[12])


def encode_register_ack(ack):
    return _REGISTER_ACK.pack(ack.assigned_node_id, ack.status, ack.server_session_id,
                              ack.heartbeat_interval_ms, ack.rssi_interval_ms, ack.caps_granted)


def decode_register_ack(payload):
    return RegisterAck(*_REGISTER_ACK.unpack(payload))


def encode_rssi_update(readings, unit=RSSI_CODE):
    '''readings = {channel(1-based): value|None}；未出现在表里的通道不进掩码。'''
    mask = 0
    for channel in sorted(readings):
        mask |= 1 << (channel - 1)
    fmt = _RSSI_FORMAT[unit]
    sentinel = NO_READING[unit]
    body = b''.join(struct.pack(fmt, sentinel if readings[ch] is None else readings[ch])
                    for ch in sorted(readings))
    return struct.pack('>H', mask) + body


def decode_rssi_update(payload, unit=RSSI_CODE):
    (mask,) = struct.unpack_from('>H', payload)
    fmt = _RSSI_FORMAT[unit]
    width = struct.calcsize(fmt)
    sentinel = NO_READING[unit]
    readings = {}
    position = 2
    remaining = mask
    while remaining:
        low = remaining & -remaining
        channel = low.bit_length()
        (value,) = struct.unpack_from(fmt, payload, position)
        readings[channel] = None if value == sentinel else value
        position += width
        remaining ^= low
    return RssiUpdate(ch_mask=mask, readings=readings)


def encode_lap_event(pass_start_uptime_ms, duration_ms, rssi_peak, rssi_threshold,
                     signal_quality, event_kind, event_id):
    return _LAP_EVENT.pack(pass_start_uptime_ms, duration_ms, rssi_peak, rssi_threshold,
                           signal_quality, event_kind, event_id)


def decode_lap_event(payload):
    return LapEvent(*_LAP_EVENT.unpack(payload))


def encode_lap_ack(ack):
    return _LAP_ACK.pack(ack.event_id, ack.status, ack.ch_mask_acked)


def decode_lap_ack(payload):
    return LapAck(*_LAP_ACK.unpack(payload))


def encode_time_sync_request(sample_index, sample_count):
    return _SYNC_REQUEST.pack(sample_index, sample_count)


def decode_time_sync_request(payload):
    return _SYNC_REQUEST.unpack(payload)


def encode_time_sync_response(echo_t1, t3_uptime_ms, sample_index):
    return _SYNC_RESPONSE.pack(echo_t1, t3_uptime_ms, sample_index)


def decode_time_sync_response(payload):
    return TimeSyncResponse(*_SYNC_RESPONSE.unpack(payload))


def encode_time_sync_result(result):
    return _SYNC_RESULT.pack(result.offset_ms, result.delay_dispersion_ms,
                             result.samples_used, result.status)


def decode_time_sync_result(payload):
    return TimeSyncResult(*_SYNC_RESULT.unpack(payload))


def sync_offset_and_delay(t1, t2, t3, t4):
    '''SPEC §6.3 四时间戳模型，返回 (offset_ms, delay_ms)。

    T1/T4 取服务器 session 单调 ms，T2/T3 取节点 uptime_ms。跨轴减法只允许发生在这里，
    "仅日志"的 uptime_ms 一律不得参与计时判定（T-1）。
    '''
    return ((t2 - t1) + (t3 - t4)) // 2, (t4 - t1) - (t3 - t2)


def encode_rf_status(status):
    return _RF_STATUS.pack(status.freq_mhz, status.locked, status.rssi_now)


def decode_rf_status(payload):
    return RfStatus(*_RF_STATUS.unpack(payload))


def encode_node_status(status):
    return _NODE_STATUS.pack(status.state, status.active_mask, status.events_buffered,
                             status.events_retransmitted, status.buffer_overflows,
                             status.crc_errors_tx, status.last_error_code)


def decode_node_status(payload):
    return NodeStatus(*_NODE_STATUS.unpack(payload))


def encode_config(items):
    '''items = [(key, value)]，1..8 项；GET 时 value 必须是 0 占位。'''
    if not 1 <= len(items) <= MAX_CONFIG_ITEMS:
        raise ValueError('config item count out of 1..{}: {}'.format(
            MAX_CONFIG_ITEMS, len(items)))
    return bytes([len(items)]) + b''.join(_CONFIG_ITEM.pack(key, value)
                                          for key, value in items)


def decode_config(payload):
    count = payload[0]
    return [_CONFIG_ITEM.unpack_from(payload, 1 + index * 3) for index in range(count)]


def encode_config_ack(ack):
    return (bytes([ack.status, len(ack.items)]) +
            b''.join(bytes([key, status]) for key, status in ack.items))


def decode_config_ack(payload):
    count = payload[1]
    return ConfigAck(status=payload[0],
                     items=[(payload[2 + i * 2], payload[3 + i * 2]) for i in range(count)])


def encode_error(error):
    return _ERROR.pack(error.error_code, error.related_msg_type, error.detail)


def decode_error(payload):
    return NodeError(*_ERROR.unpack(payload))


def encode_log(level, line):
    raw = line.encode('utf-8')
    if len(raw) > MAX_LOG_LEN:
        raise ValueError('log line exceeds {} bytes: {}'.format(MAX_LOG_LEN, len(raw)))
    return bytes([level, len(raw)]) + raw


def decode_log(payload):
    return payload[0], payload[2:2 + payload[1]].decode('utf-8')


def encode_reboot(reboot):
    return _REBOOT.pack(reboot.confirm_token, reboot.delay_ms)


def decode_reboot(payload):
    return Reboot(*_REBOOT.unpack(payload))
