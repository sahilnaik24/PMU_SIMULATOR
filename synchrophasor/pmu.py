import logging
import socket

from select import select
from threading import Thread
from queue import Queue, Full
from sys import stdout
from time import sleep, time
from synchrophasor.frame import *

__author__ = "Stevan Sandi"
__copyright__ = "Copyright (c) 2016, Tomo Popovic, Stevan Sandi, Bozo Krstajic"
__credits__ = []
__license__ = "BSD-3"
__version__ = "1.0.0-alpha"


class DroppingQueue(Queue):
    def __init__(self, maxsize):
        super().__init__(maxsize=maxsize)

    def put(self, item):
        try:
            super().put(item, block=False)
        except Full:
            # queue is full → discard the oldest frame
            try:
                _ = self.get_nowait()
            except Exception:
                pass
            # now there’s room for the new one
            super().put(item, block=False)

class Pmu(object):

    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stdout)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    def __init__(self, pmu_id=7734, data_rate=30, port=4712, ip="127.0.0.1",
                 method="tcp", buffer_size=2048, set_timestamp=True):

        self.port = port
        self.ip = ip

        self.socket = None
        self.listener = None
        self.set_timestamp = set_timestamp
        self.buffer_size = buffer_size

        # Flag to indicate that the PMU is stopped.
        self._stopped = False

        self.ieee_cfg2_sample = ConfigFrame2(
            pmu_id, 1000000, 1, "Station A", 7734,
            (False, False, True, False),
            4, 3, 1,
            ["VA", "VB", "VC", "I1", "ANALOG1", "ANALOG2", "ANALOG3",
             "BREAKER 1 STATUS", "BREAKER 2 STATUS", "BREAKER 3 STATUS",
             "BREAKER 4 STATUS", "BREAKER 5 STATUS", "BREAKER 6 STATUS",
             "BREAKER 7 STATUS", "BREAKER 8 STATUS", "BREAKER 9 STATUS",
             "BREAKER A STATUS", "BREAKER B STATUS", "BREAKER C STATUS",
             "BREAKER D STATUS", "BREAKER E STATUS", "BREAKER F STATUS",
             "BREAKER G STATUS"],
            [(915527, "v"), (915527, "v"), (915527, "v"), (45776, "i")],
            [(1, "pow"), (1, "rms"), (1, "peak")],
            [(0x0000, 0xffff)],
            60, 22, data_rate)

        self.ieee_data_sample = DataFrame(
            pmu_id,
            ("ok", True, "timestamp", False, False, False, 0, "<10", 0),
            [(14635, 0), (-7318, -12676), (-7318, 12675), (1092, 0)],
            2500, 0,
            [100, 1000, 10000],
            [0x3c12],
            self.ieee_cfg2_sample)

        self.ieee_command_sample = CommandFrame(pmu_id, "start", None)

        self.cfg1 = self.ieee_cfg2_sample
        self.cfg1.__class__ = ConfigFrame1   # Casting CFG2 to CFG1
        self.cfg2 = self.ieee_cfg2_sample
        self.cfg3 = None
        self.header = HeaderFrame(pmu_id, "Hi! I am tinyPMU!")

        self.method = method
        self.clients = []
        self.client_buffers = []

    def set_id(self, pmu_id):
        self.cfg1.set_id_code(pmu_id)
        self.cfg2.set_id_code(pmu_id)
        self.send(self.cfg2)
        self.logger.info("[%d] - PMU Id changed.", self.cfg2.get_id_code())

    def set_configuration(self, config=None):
        if not config:
            self.cfg1 = self.ieee_cfg2_sample
            self.cfg1.__class__ = ConfigFrame1
            self.cfg2 = self.ieee_cfg2_sample
            self.cfg3 = None
        elif type(config) == ConfigFrame1:
            self.cfg1 = config
        elif type(config) == ConfigFrame2:
            self.cfg2 = config
            if not self.cfg1:
                self.cfg1 = config
                self.cfg1.__class__ = ConfigFrame1
        elif type(config) == ConfigFrame3:
            self.cfg3 = ConfigFrame3
        else:
            raise PmuError("Incorrect configuration!")

        self.logger.info("[%d] - PMU configuration changed.", self.cfg2.get_id_code())

    def set_header(self, header=None):
        if header is None:
            # If no header is provided, simply return (or you could choose to use a default header)
            return
        elif isinstance(header, HeaderFrame):
            self.header = header
        elif isinstance(header, str):
            self.header = HeaderFrame(self.cfg2.get_id_code(), header)
        else:
            raise PmuError("Incorrect header setup! Only HeaderFrame and string allowed.")

    def set_data_rate(self, data_rate):
        self.cfg1.set_data_rate(data_rate)
        self.cfg2.set_data_rate(data_rate)
        self.data_rate = data_rate
        self.send(self.cfg2)
        self.logger.info("[%d] - PMU reporting data rate changed.", self.cfg2.get_id_code())

    def set_data_format(self, data_format):
        self.cfg1.set_data_format(data_format, self.cfg1.get_num_pmu())
        self.cfg2.set_data_format(data_format, self.cfg2.get_num_pmu())
        self.send(self.cfg2)
        self.logger.info("[%d] - PMU data format changed.", self.cfg2.get_id_code())

    def send(self, frame):
        if not isinstance(frame, CommonFrame) and not isinstance(frame, bytes):
            raise PmuError("Invalid frame type. send() method accepts only frames or raw bytes.")
        for buffer in self.client_buffers:
            buffer.put(frame)

    def send_data(self, phasors=[], analog=[], digital=[], freq=0, dfreq=0,
                  stat=("ok", True, "timestamp", False, False, False, 0, "<10", 0), soc=None, frasec=None):
        data_frame = DataFrame(self.cfg2.get_id_code(), stat, phasors, freq, dfreq, analog, digital, self.cfg2)
        for buffer in self.client_buffers:
            buffer.put(data_frame)

    def run(self):
        if not self.cfg1 and not self.cfg2 and not self.cfg3:
            raise PmuError("Cannot run PMU without configuration.")
        self._stopped = False
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.ip, self.port))
        self.socket.listen(5)
        self.listener = Thread(target=self.acceptor)
        self.listener.daemon = True
        self.listener.start()
    


    def acceptor(self):
        while not self._stopped:
            try:
                self.logger.info("[%d] - Waiting for connection on %s:%d", 
                                 self.cfg2.get_id_code(), self.ip, self.port)
                conn, address = self.socket.accept()
            except OSError as e:
                # If the socket is closed or not valid, exit the loop.
                if self._stopped or (hasattr(e, 'errno') and e.errno == 10038):
                    break
                else:
                    self.logger.error("OSError in acceptor: %s", e)
                    break


            # buffer = Queue()
            buffer = DroppingQueue(maxsize=500)
            self.client_buffers.append(buffer)

            thread = Thread(
                target=self.pdc_handler,
                args=(
                    conn, address, buffer,
                    self.cfg2.get_id_code(),
                    self.cfg2.get_data_rate(),
                    self.cfg1, self.cfg2,
                    self.cfg3, self.header,
                    self.buffer_size,
                    self.set_timestamp,
                    self.logger.level
                ),
                daemon=True                 # note the comma above!
            )
            thread.start()
            self.clients.append(thread)


            # conn.close()  #---> used when multiprocess, not thread

    def join(self):
        while self.listener.is_alive():
            self.listener.join(0.5)

    def stop(self):
        """Stop the PMU by closing the socket, joining the listener thread,
           and terminating client processes."""
        self._stopped = True
        try:
            if self.socket:
                self.socket.close()
                self.socket = None
            if self.listener and self.listener.is_alive():
                self.listener.join(timeout=1)
            for client in self.clients:
                client.terminate()
            self.clients = []
            self.logger.info("[%d] - PMU stopped.", self.cfg2.get_id_code())
        except Exception as e:
            self.logger.error("Error while stopping PMU: %s", e)



    # New method with thread.
    @staticmethod
    def pdc_handler(connection, address, buffer, pmu_id, data_rate,
                    cfg1, cfg2, cfg3, header, buffer_size,
                    set_timestamp, log_level):
        logger = logging.getLogger(f"{address[0]}:{address[1]}")
        logger.setLevel(log_level)
        handler = logging.StreamHandler(stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)

        logger.info("[%d] - Connection from %s:%d", pmu_id, address[0], address[1])
        sending_measurements = False
        delay = 1.0 / data_rate if data_rate > 0 else -data_rate

        try:
            while True:
                command = None
                received_data = b""
                # wait for any incoming data
                ready, _, _ = select([connection], [], [], 0)
                if ready:
                    # read frame length prefix
                    while len(received_data) < 4:
                        chunk = connection.recv(buffer_size)
                        if not chunk:
                            raise ConnectionError("Client disconnected during header read")
                        received_data += chunk

                    total_size = int.from_bytes(received_data[2:4], "big")
                    # read until full frame
                    while len(received_data) < total_size:
                        chunk = connection.recv(min(buffer_size, total_size - len(received_data)))
                        if not chunk:
                            raise ConnectionError("Client disconnected mid-frame")
                        received_data += chunk

                    # parse into a frame object
                    try:
                        frame = CommonFrame.convert2frame(received_data)
                    except FrameError:
                        logger.warning("[%d] - Bad frame from %s:%d", pmu_id, *address)
                        continue

                    if isinstance(frame, CommandFrame):
                        command = frame.get_command()
                        logger.info("[%d] - Cmd [%s] from %s:%d", pmu_id, command, *address)
                    else:
                        logger.info("[%d] - %s from %s:%d",
                                    pmu_id, type(frame).__name__, *address)

                # handle commands
                if command:
                    if command == "start":
                        sending_measurements = True
                    elif command == "stop":
                        sending_measurements = False
                    elif command in ("header", "cfg1", "cfg2", "cfg3"):
                        target = {
                            "header": header,
                            "cfg1": cfg1,
                            "cfg2": cfg2,
                            "cfg3": cfg3
                        }[command]
                        if target:
                            if set_timestamp:
                                target.set_time()
                            connection.sendall(target.convert2bytes())
                            logger.info("[%d] - Sent %s to %s:%d",
                                        pmu_id, command, *address)

                # send measurement frames from buffer
                # print("Sending measurements:", sending_measurements,"buffer empty:", buffer.empty())
                if sending_measurements and not buffer.empty():
                    data = buffer.get()
                    if isinstance(data, CommonFrame):
                        if set_timestamp:
                            data.set_time()
                        data = data.convert2bytes()
                    sleep(delay)
                    connection.sendall(data)
                    logger.debug("[%d] - Sent data at %f to %s:%d",
                                pmu_id, time(), *address)

        except ConnectionError as ce:
            logger.info("[%d] - Client %s:%d disconnected: %s", pmu_id, *address, ce)
        except Exception:
            logger.exception("[%d] - Unexpected error in handler for %s:%d", pmu_id, *address)
        finally:
            # ensure socket is shut down and closed exactly once
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            connection.close()
            logger.info("[%d] - Closed connection to %s:%d", pmu_id, *address)

class PmuError(BaseException):
    pass
