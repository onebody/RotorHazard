import logging
import socket
from time import monotonic

from Node import Node
from RHInterface import READ_ADDRESS, READ_REVISION_CODE, MAX_RETRY_COUNT, \
                        READ_FW_VERSION, READ_FW_BUILDDATE, READ_FW_BUILDTIME, \
                        FW_TEXT_BLOCK_SIZE, validate_checksum, calculate_checksum, \
                        pack_16, unpack_16, READ_FW_PROCTYPE, SEND_STATUS_MESSAGE

logger = logging.getLogger(__name__)

class I2CNode(Node):
    def __init__(self, index, addr, host, port):
        Node.__init__(self)
        self.index = index
        self.addr = addr
        self.host = host
        self.port = port
        self.socket = None

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
            if data:
                self.socket.sendall(pack_16(command) + data)
            else:
                self.socket.sendall(pack_16(command))
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
        self.inc_read_block_count()
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
                    self.inc_read_error_count()
            except socket.error as e:
                logger.error(f"Read Error: {e}")
                self.disconnect()
                retry_count = retry_count + 1
                if retry_count <= max_retries:
                    if retry_count > 1:
                        logger.warning(f"Retry (IOError) in read_block: addr={self.addr} cmd={command} size={size} retry={retry_count}")
                else:
                    logger.error(f"Retry (IOError) limit reached in read_block: addr={self.addr} cmd={command} size={size} retry={retry_count}")
                self.inc_read_error_count()
        return data

    def write_block(self, command, data):
        self.inc_intf_write_block_count()
        success = False
        retry_count = 0
        data_with_checksum = data
        if self.api_level <= 19:
            data_with_checksum.append(command)
        data_with_checksum.append(calculate_checksum(data_with_checksum))
        while success is False and retry_count <= MAX_RETRY_COUNT:
            try:
                self.send_command(command, data_with_checksum)
                success = True
            except socket.error as e:
                logger.error(f"Write Error: {e}")
                self.disconnect()
                retry_count = retry_count + 1
                if retry_count <= MAX_RETRY_COUNT:
                    logger.warning(f"Retry (IOError) in write_block: addr={self.addr} cmd={command} data={data} retry={retry_count}")
                else:
                    logger.error(f"Retry (IOError) limit reached in write_block: addr={self.addr} cmd={command} data={data} retry={retry_count}")
                self.inc_intf_write_error_count()
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
