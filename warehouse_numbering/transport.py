"""Asynchronous single-request serial transport. It never resends a request."""

from PySide6.QtCore import QIODevice, QObject, QTimer, Signal
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo

from .protocol import FixtureError, ReplyBuffer, SUCCESS_LENGTH, decode_reply, make_request


class FixtureTransport(QObject):
    disconnected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.serial = QSerialPort(self)
        self.serial.readyRead.connect(self._receive)
        self.serial.errorOccurred.connect(self._serial_error)
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self._timed_out)
        self.quiet = QTimer(self)
        self.quiet.setSingleShot(True)
        self.quiet.setInterval(500)
        self._pending = None
        self._connected = False
        self._bits = 10
        self._baud = 1200

    @staticmethod
    def available_ports():
        return [info.portName() for info in QSerialPortInfo.availablePorts()]

    def open(self, port_name, baud, parity, stop_bits):
        self.close()
        parity_mode = {"N": QSerialPort.Parity.NoParity, "E": QSerialPort.Parity.EvenParity,
                       "O": QSerialPort.Parity.OddParity}[parity]
        stop_mode = QSerialPort.StopBits.OneStop if stop_bits == 1 else QSerialPort.StopBits.TwoStop
        self.serial.setPortName(port_name)
        self.serial.setBaudRate(baud)
        self.serial.setDataBits(QSerialPort.DataBits.Data8)
        self.serial.setParity(parity_mode)
        self.serial.setStopBits(stop_mode)
        self.serial.setFlowControl(QSerialPort.FlowControl.NoFlowControl)
        if not self.serial.open(QIODevice.OpenModeFlag.ReadWrite):
            raise FixtureError(f"无法打开串口 {port_name}。{self.serial.errorString()}")
        actual = (self.serial.baudRate(), self.serial.dataBits(), self.serial.parity(),
                  self.serial.stopBits(), self.serial.flowControl())
        if actual != (baud, QSerialPort.DataBits.Data8, parity_mode, stop_mode, QSerialPort.FlowControl.NoFlowControl):
            self.close()
            raise FixtureError("串口实际参数与所选设置不一致。")
        self.serial.setDataTerminalReady(False)
        self.serial.setRequestToSend(False)
        self.serial.clear()
        self._baud = baud
        self._bits = 1 + 8 + (parity != "N") + stop_bits
        self._connected = True

    def close(self):
        self._connected = False
        self.timeout.stop()
        self.quiet.stop()
        self._pending = None
        if self.serial.isOpen():
            self.serial.close()

    def request(self, address, op, payload, token, callback):
        if not self._connected or not self.serial.isOpen() or self._pending is not None or self.quiet.isActive():
            raise FixtureError("串口未连接或仍在等待上一条响应，请稍后读取。")
        frame = make_request(address, op, payload)
        self.serial.readAll()
        self._pending = (ReplyBuffer(address, op, token), callback)
        wire_ms = ((len(frame) + 6 + SUCCESS_LENGTH[op]) * self._bits * 1000 + self._baud - 1) // self._baud
        self.timeout.start(4500 + wire_ms)
        sent = self.serial.write(frame)
        if sent != len(frame) and self._pending is not None:
            self.quiet.start()
            self._finish(error=FixtureError("工装请求未完整发送，操作结果待核验。"))

    def _receive(self):
        data = bytes(self.serial.readAll())
        if self._pending is None:
            if self.quiet.isActive():
                self.quiet.start()
            return
        parser, _ = self._pending
        try:
            reply = parser.feed(data)
            if reply is None:
                return
            value = decode_reply(parser.op, *reply, token=parser.token)
        except ValueError as error:
            self._finish(error=error)
        else:
            self._finish(value=value)

    def _finish(self, value=None, error=None):
        if self._pending is None:
            return
        _, callback = self._pending
        self._pending = None
        self.timeout.stop()
        callback(value, error)

    def _timed_out(self):
        if self._pending is None:
            return
        parser, _ = self._pending
        self.quiet.start()
        message = "工装响应 CRC 校验失败。" if parser.bad_crc else "等待工装响应超时。"
        self._finish(error=FixtureError(message))

    def _serial_error(self, error):
        fatal = (QSerialPort.SerialPortError.ResourceError, QSerialPort.SerialPortError.DeviceNotFoundError,
                 QSerialPort.SerialPortError.ReadError, QSerialPort.SerialPortError.WriteError,
                 QSerialPort.SerialPortError.PermissionError)
        if error not in fatal or not self._connected:
            return
        message = self.serial.errorString()
        self.close()
        self.disconnected.emit(message)
