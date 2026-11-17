import logging
import socket
from time import monotonic

from Node import Node
from RHInterface import READ_ADDRESS, READ_REVISION_CODE, MAX_RETRY_COUNT, \
                        READ_FW_VERSION, READ_FW_BUILDDATE, READ_FW_BUILDTIME, \
                        FW_TEXT_BLOCK_SIZE, validate_checksum, calculate_checksum, \
                        pack_16, unpack_16, READ_FW_PROCTYPE, SEND_STATUS_MESSAGE

logger = logging.getLogger(__name__)


def discover(*args, **kwargs):
    # 扫描器按 *_node 后缀 import 本模块并调 discover()（见 interface/Plugins.py）。
    # 该接口目前没有配置入口、也没有调用方，返回空表以免每次启动记 ERROR。
    return []


class I2CNode(Node):
    def __init__(self, index, addr, host, port):
        Node.__init__(self)
        self.index = index
        self.addr = addr
        self.host = host
        self.port = port
        self.socket = None
        # Node 基类的 inc_*_count 需要一个 interface 参数（socket 节点没有），
        # 这里各自计数。
        self.write_block_count = 0
        self.write_error_count = 0

    def connect(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.host, self.port))

    def disconnect(self):
        if self.socket:
            self.socket.close()
            self.socket = None

    def send_command(self, command, data=None):
        if not self.socket:
            self.connect()
        try:
            # pack_16 返回 [high, low] 列表，走 socket 前要转成字节。
            head = bytes(pack_16(command))
            self.socket.sendall(head + data if data else head)
        except socket.error as e:
            logger.error(f"Error sending command {command}: {e}")
            self.disconnect()
            raise

    def receive_response(self, size):
        if not self.socket:
            self.connect()
        data = bytearray()
        while len(data) < size:
            try:
                chunk = self.socket.recv(size - len(data))
                if not chunk:
                    raise socket.error("Connection closed by remote host")
                data.extend(chunk)
            except socket.error as e:
                logger.error(f"Error receiving response: {e}")
                self.disconnect()
                raise
        return data

    def read_block(self, command, size, max_retries=MAX_RETRY_COUNT):
        self.read_block_count += 1
        success = False
        retry_count = 0
        data = None
        while success is False and retry_count <= max_retries:
            try:
                self.send_command(command, None)
                data = self.receive_response(size + 1)
                if validate_checksum(data):
                    success = True
                    data = data[:-1]
                else:
                    retry_count = retry_count + 1
                    if retry_count <= max_retries:
                        if retry_count > 1:
                            logger.warning(f"Retry (checksum) in read_block: addr={self.addr} cmd={command} size={size} retry={retry_count}")
                    else:
                        logger.error(f"Retry (checksum) limit reached in read_block: addr={self.addr} cmd={command} size={size} retry={retry_count}")
                    self.read_error_count += 1
            except socket.error as e:
                logger.error(f"Read Error: {e}")
                self.disconnect()
                retry_count = retry_count + 1
                if retry_count <= max_retries:
                    if retry_count > 1:
                        logger.warning(f"Retry (IOError) in read_block: addr={self.addr} cmd={command} size={size} retry={retry_count}")
                else:
                    logger.error(f"Retry (IOError) limit reached in read_block: addr={self.addr} cmd={command} size={size} retry={retry_count}")
                self.read_error_count += 1
        return data if success else None

    def write_block(self, command, data):
        self.write_block_count += 1
        success = False
        retry_count = 0
        payload = bytearray(data)
        if self.api_level <= 19:
            payload.append(command)
        payload.append(calculate_checksum(payload))
        while success is False and retry_count <= MAX_RETRY_COUNT:
            try:
                self.send_command(command, bytes(payload))
                success = True
            except socket.error as e:
                logger.error(f"Write Error: {e}")
                self.disconnect()
                retry_count = retry_count + 1
                if retry_count <= MAX_RETRY_COUNT:
                    logger.warning(f"Retry (IOError) in write_block: addr={self.addr} cmd={command} data={data} retry={retry_count}")
                else:
                    logger.error(f"Retry (IOError) limit reached in write_block: addr={self.addr} cmd={command} data={data} retry={retry_count}")
                self.write_error_count += 1
        return success

    def jump_to_bootloader(self):
        self.send_command(0x01)

    def read_firmware_version(self):
        data = self.read_block(READ_FW_VERSION, FW_TEXT_BLOCK_SIZE, 2)
        self.firmware_version_str = bytearray(data).decode("utf-8").rstrip('\0') if data else None

    def read_firmware_proctype(self):
        data = self.read_block(READ_FW_PROCTYPE, FW_TEXT_BLOCK_SIZE, 2)
        self.firmware_proctype_str = bytearray(data).decode("utf-8").rstrip('\0') if data else None

    def read_firmware_timestamp(self):
        data = self.read_block(READ_FW_BUILDDATE, FW_TEXT_BLOCK_SIZE, 2)
        if data:
            self.firmware_timestamp_str = bytearray(data).decode("utf-8").rstrip('\0')
            data = self.read_block(READ_FW_BUILDTIME, FW_TEXT_BLOCK_SIZE, 2)
            if data:
                self.firmware_timestamp_str += " " + bytearray(data).decode("utf-8").rstrip('\0')
        else:
            self.firmware_timestamp_str = None

    def send_status_message(self, msgTypeVal, msgDataVal):
        if self.api_level >= 35:
            data = ((msgTypeVal & 0xFF) << 8) | (msgDataVal & 0xFF)
            self.write_block(SEND_STATUS_MESSAGE, pack_16(data))
