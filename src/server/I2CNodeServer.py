import logging
import socket
from time import monotonic

from RHInterface import READ_ADDRESS, READ_REVISION_CODE, MAX_RETRY_COUNT, \
                        READ_FW_VERSION, READ_FW_BUILDDATE, READ_FW_BUILDTIME, \
                        FW_TEXT_BLOCK_SIZE, validate_checksum, calculate_checksum, \
                        pack_16, unpack_16, READ_FW_PROCTYPE, SEND_STATUS_MESSAGE

logger = logging.getLogger(__name__)

class I2CNodeServer:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.socket = None

    def start(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind((self.host, self.port))
        self.socket.listen(5)
        logger.info(f"Server started on {self.host}:{self.port}")

    def stop(self):
        if self.socket:
            self.socket.close()
            self.socket = None
            logger.info("Server stopped")

    def handle_client(self, client_socket):
        try:
            while True:
                # 接收命令
                command = client_socket.recv(2)
                if not command:
                    break
                command = unpack_16(command)

                # 根据命令处理数据
                if command == READ_ADDRESS:
                    # 处理读取地址的命令
                    response = self.read_address()
                elif command == READ_REVISION_CODE:
                    # 处理读取版本代码的命令
                    response = self.read_revision_code()
                elif command == READ_FW_VERSION:
                    # 处理读取固件版本的命令
                    response = self.read_firmware_version()
                elif command == READ_FW_BUILDDATE:
                    # 处理读取固件构建日期的命令
                    response = self.read_firmware_builddate()
                elif command == READ_FW_BUILDTIME:
                    # 处理读取固件构建时间的命令
                    response = self.read_firmware_buildtime()
                elif command == READ_FW_PROCTYPE:
                    # 处理读取固件处理器类型的命令
                    response = self.read_firmware_proctype()
                elif command == SEND_STATUS_MESSAGE:
                    # 处理发送状态消息的命令
                    data = client_socket.recv(2)
                    if not data:
                        break
                    msgTypeVal, msgDataVal = unpack_16(data)
                    response = self.send_status_message(msgTypeVal, msgDataVal)
                else:
                    # 未知命令
                    response = b''

                # 发送响应
                client_socket.sendall(response)

        except socket.error as e:
            logger.error(f"Error handling client: {e}")
        finally:
            client_socket.close()

    def read_address(self):
        # 实现读取地址的逻辑
        return b'\x01\x02'  # 示例响应

    def read_revision_code(self):
        # 实现读取版本代码的逻辑
        return b'\x03\x04'  # 示例响应

    def read_firmware_version(self):
        # 实现读取固件版本的逻辑
        return b'Firmware Version 1.0'  # 示例响应

    def read_firmware_builddate(self):
        # 实现读取固件构建日期的逻辑
        return b'2021-01-01'  # 示例响应

    def read_firmware_buildtime(self):
        # 实现读取固件构建时间的逻辑
        return b'12:00:00'  # 示例响应

    def read_firmware_proctype(self):
        # 实现读取固件处理器类型的逻辑
        return b'Processor Type'  # 示例响应

    def send_status_message(self, msgTypeVal, msgDataVal):
        # 实现发送状态消息的逻辑
        logger.info(f"Received status message: {msgTypeVal}, {msgDataVal}")
        return b''  # 示例响应

if __name__ == "__main__":
    server = I2CNodeServer('localhost', 8888)
    server.start()

    try:
        while True:
            client_socket, addr = server.socket.accept()
            logger.info(f"Accepted connection from {addr}")
            server.handle_client(client_socket)
    except KeyboardInterrupt:
        server.stop()
