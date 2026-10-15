"""
NFC Desktop App (PyQt5) - SINGLE FILE
=====================================
Ứng dụng desktop cho đầu đọc NFC-PM5 (PN532). TẤT CẢ code trong 1 file.
Chỉ cần file này + thư mục lang/ (vi.json, en.json, ja.json) là chạy được.

  * CHẾ ĐỘ INPUT : chạm thẻ -> GÕ nội dung (NDEF URL / UID) vào ô focus.
  * CHẾ ĐỘ READ  : đọc liên tục, hiện UID + NDEF.
  * CHẾ ĐỘ WRITE : ghi liên tục, cứ chạm thẻ mới là ghi.

- Chỉ 1 chế độ active tại 1 thời điểm (thanh chế độ chung ở trên).
- Chạy ngầm ở tray; đa ngôn ngữ (vi/en/ja) - bản dịch ở thư mục lang/.

ĐA NỀN TẢNG (Windows / Linux / macOS):
- Windows: yêu cầu quyền Administrator (tự xin qua UAC) để thư viện `keyboard`
  gõ phím vào app khác; tự ẩn console bằng pythonw.exe.
- Linux/macOS: chạy thẳng (không UAC); chế độ Input dùng `pynput`.
  Linux có thể cần chạy bằng sudo; macOS cần cấp quyền Accessibility.
- Cổng thiết bị: Windows là COMx, Linux là /dev/ttyUSBx, macOS là /dev/tty.usbserial-*.

Chạy:  python nfc_app.py
Phụ thuộc: PyQt5, pyserial, và (keyboard trên Windows / pynput trên Linux-macOS)
           + thư mục lang/ chứa các file JSON.
"""
import sys
import os
import time
import queue
import ctypes
import serial
import serial.tools.list_ports
from enum import Enum
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QTextEdit, QTabWidget, QComboBox, QCheckBox,
    QSystemTrayIcon, QMenu, QStyle, QGroupBox, QRadioButton, QMessageBox,
    QDialog, QDialogButtonBox, QListWidget, QListWidgetItem,
    QButtonGroup, QFrame, QFileDialog,
)

# =====================================================================
#  ĐA NỀN TẢNG: cờ hệ điều hành + lớp gõ phím trừu tượng
# =====================================================================
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


class _KeyTyper:
    """
    Lớp trừu tượng gõ phím vào ứng dụng đang focus, hoạt động đa nền tảng.

    - Windows: dùng thư viện `keyboard` (ổn định với bộ gõ tiếng Việt / layout,
      tránh lỗi ký tự thừa; cần quyền Administrator để gõ vào app khác).
    - Linux / macOS: dùng `pynput` (keyboard của thư viện `keyboard` cần root
      trên Linux và gần như không dùng được trên macOS).

    Thư viện được import lazy để app vẫn chạy (các chế độ Đọc/Ghi) ngay cả khi
    thư viện gõ phím chưa được cài.
    """

    def __init__(self):
        self._backend = None   # "keyboard" | "pynput" | None
        self._kb = None        # module keyboard
        self._pynput_ctrl = None
        self._pynput_key = None
        self._init_error = None

    def _ensure(self) -> bool:
        """Khởi tạo backend lần đầu khi cần. Trả về True nếu sẵn sàng gõ."""
        if self._backend:
            return True
        if IS_WINDOWS:
            try:
                import keyboard
                self._kb = keyboard
                self._backend = "keyboard"
                return True
            except Exception as e:
                self._init_error = f"keyboard: {e}"
                return False
        else:
            try:
                from pynput.keyboard import Controller, Key
                self._pynput_ctrl = Controller()
                self._pynput_key = Key
                self._backend = "pynput"
                return True
            except Exception as e:
                self._init_error = f"pynput: {e}"
                return False

    def type_text(self, text: str):
        """Gõ một chuỗi văn bản vào ô đang focus."""
        if not self._ensure():
            return
        if self._backend == "keyboard":
            self._kb.write(text, delay=0.01)
        else:
            self._pynput_ctrl.type(text)

    def press_key(self, key_name: str):
        """
        Nhấn 1 phím chức năng/điều hướng theo tên chuẩn:
        tab, space, enter, up, down, left, right.
        """
        if not self._ensure():
            return
        if self._backend == "keyboard":
            self._kb.press_and_release(key_name)
        else:
            Key = self._pynput_key
            mapping = {
                "tab": Key.tab, "space": Key.space, "enter": Key.enter,
                "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
            }
            k = mapping.get(key_name)
            if k is not None:
                self._pynput_ctrl.press(k)
                self._pynput_ctrl.release(k)


# Thể hiện dùng chung cho toàn app
_typer = _KeyTyper()


# =====================================================================
#  NFC-PM5 (PN532) DRIVER  -- gộp từ nfc_pm5.py
# =====================================================================

# ===== PN532 Constants =====
PN532_PREAMBLE = 0x00
PN532_STARTCODE1 = 0x00
PN532_STARTCODE2 = 0xFF
PN532_POSTAMBLE = 0x00

PN532_HOST_TO_PN532 = 0xD4
PN532_PN532_TO_HOST = 0xD5

# PN532 Commands
PN532_CMD_GETFIRMWAREVERSION = 0x02
PN532_CMD_SAMCONFIGURATION = 0x14
PN532_CMD_INLISTPASSIVETARGET = 0x4A
PN532_CMD_INDATAEXCHANGE = 0x40
PN532_CMD_POWERDOWN = 0x16

# MIFARE Commands
MIFARE_CMD_AUTH_A = 0x60
MIFARE_CMD_AUTH_B = 0x61
MIFARE_CMD_READ = 0x30
MIFARE_CMD_WRITE = 0xA0

# NTAG / MIFARE Ultralight Commands
NTAG_CMD_READ = 0x30       # Đọc 16 bytes (4 pages), bắt đầu từ page chỉ định
NTAG_CMD_WRITE = 0xA2      # Ghi 4 bytes vào 1 page

# NDEF URI prefix codes (NFCForum-TS-RTD_URI_1.0)
NDEF_URI_PREFIX = {
    0x00: "",
    0x01: "http://www.",
    0x02: "https://www.",
    0x03: "http://",
    0x04: "https://",
}

# Default key
DEFAULT_KEY = bytes([0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])

# Baud rate
BAUD_RATE = 115200
TIMEOUT = 2


class NFCPM5:
    """Interface for NFC-PM5 (PN532-based) reader/writer."""

    def __init__(self, port: str = "COM3", baud_rate: int = BAUD_RATE):
        self.port = port
        self.baud_rate = baud_rate
        self.ser: Optional[serial.Serial] = None
        self.uid: Optional[bytes] = None
        self.uid_length: int = 0

    def connect(self) -> bool:
        """Kết nối với thiết bị NFC-PM5."""
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=TIMEOUT,
            )
            time.sleep(0.5)
            print(f"[+] Da ket noi toi {self.port} @ {self.baud_rate} baud")

            # Khởi tạo PN532
            if not self._sam_configuration():
                print("[-] Khong the cau hinh SAM")
                return False

            # Lấy phiên bản firmware
            self._get_firmware_version()
            return True

        except serial.SerialException as e:
            print(f"[-] Loi ket noi: {e}")
            return False

    def disconnect(self):
        """Ngắt kết nối."""
        if self.ser and self.ser.is_open:
            self.ser.close()
            print("[+] Da ngat ket noi")

    # ===== Low-level PN532 Protocol =====

    def _write_frame(self, data: bytes):
        """Gửi 1 frame PN532."""
        length = len(data) + 1  # TFI + data
        lcs = (~length + 1) & 0xFF  # Checksum độ dài

        frame = bytes([
            PN532_PREAMBLE,
            PN532_STARTCODE1,
            PN532_STARTCODE2,
            length,
            lcs,
            PN532_HOST_TO_PN532,
        ]) + data

        # Checksum dữ liệu
        dcs = PN532_HOST_TO_PN532
        for b in data:
            dcs += b
        dcs = (~dcs + 1) & 0xFF

        frame += bytes([dcs, PN532_POSTAMBLE])
        self.ser.write(frame)

    def _read_ack(self) -> bool:
        """Đọc ACK frame từ PN532."""
        time.sleep(0.05)
        ack = self.ser.read(6)
        if len(ack) >= 6:
            # ACK = 00 00 FF 00 FF 00
            if ack[0:4] == bytes([0x00, 0x00, 0xFF, 0x00]):
                return True
        return False

    def _read_response(self, timeout: float = 1.0) -> Optional[bytes]:
        """Đọc response frame từ PN532. Trả về data (không bao gồm TFI)."""
        start = time.time()

        while time.time() - start < timeout:
            # Tìm preamble + startcode
            byte = self.ser.read(1)
            if not byte:
                continue

            if byte[0] == PN532_PREAMBLE:
                header = self.ser.read(2)
                if len(header) < 2:
                    continue
                if header[0] == PN532_STARTCODE1 and header[1] == PN532_STARTCODE2:
                    # Đọc length
                    length_bytes = self.ser.read(2)
                    if len(length_bytes) < 2:
                        continue
                    length = length_bytes[0]
                    lcs = length_bytes[1]

                    # Kiểm tra checksum độ dài
                    if (length + lcs) & 0xFF != 0:
                        continue

                    # Đọc TFI + data + DCS + postamble
                    payload = self.ser.read(length + 2)
                    if len(payload) < length + 1:
                        continue

                    tfi = payload[0]
                    if tfi != PN532_PN532_TO_HOST:
                        continue

                    data = payload[1:length]
                    return data

        return None

    def _send_command(self, command: int, params: bytes = b"") -> Optional[bytes]:
        """Gửi command và nhận response từ PN532."""
        if not self.ser or not self.ser.is_open:
            return None

        self.ser.reset_input_buffer()
        data = bytes([command]) + params
        self._write_frame(data)

        # Đọc ACK
        if not self._read_ack():
            return None

        # Đọc response
        response = self._read_response()
        if response and len(response) > 0:
            # response[0] phải bằng command + 1
            if response[0] == command + 1:
                return response[1:]  # Trả về data không gồm byte command

        return None

    def poll_target(self, timeout: float = 0.15) -> Optional[bytes]:
        """
        Tìm thẻ ISO14443A với TIMEOUT NGẮN để polling nhanh (không block ~2s).
        Dùng cho vòng lặp đọc/ghi liên tục. Trả về UID nếu có thẻ, ngược lại None.

        Khác _send_command ở chỗ: tạm đặt serial timeout ngắn và dùng _read_response
        với timeout ngắn, để khi KHÔNG có thẻ thì trả về nhanh thay vì chờ lâu.
        """
        if not self.ser or not self.ser.is_open:
            return None

        old_timeout = self.ser.timeout
        try:
            self.ser.timeout = timeout
            self.ser.reset_input_buffer()
            self._write_frame(bytes([PN532_CMD_INLISTPASSIVETARGET, 0x01, 0x00]))
            if not self._read_ack():
                return None
            result = self._read_response(timeout=timeout)
            if result and len(result) >= 1 and result[0] == PN532_CMD_INLISTPASSIVETARGET + 1:
                data = result[1:]
                # data: NbTg, [Tg, SENS_RES(2), SEL_RES, UIDLen, UID...]
                if len(data) >= 6 and data[0] > 0:
                    uid_len = data[5]
                    if len(data) >= 6 + uid_len:
                        return data[6:6 + uid_len]
            return None
        finally:
            self.ser.timeout = old_timeout

    # ===== PN532 High-level Commands =====

    def _sam_configuration(self) -> bool:
        """Cấu hình Security Access Module - cần gọi trước khi sử dụng."""
        # Mode: Normal (0x01), Timeout: 0, IRQ: 0
        result = self._send_command(PN532_CMD_SAMCONFIGURATION, bytes([0x01, 0x00, 0x00]))
        return result is not None

    def _get_firmware_version(self) -> Optional[tuple]:
        """Lấy phiên bản firmware của PN532."""
        result = self._send_command(PN532_CMD_GETFIRMWAREVERSION)
        if result and len(result) >= 4:
            ic = result[0]
            ver = result[1]
            rev = result[2]
            support = result[3]
            print(f"[+] PN532 Firmware: IC=0x{ic:02X}, Ver={ver}.{rev}, Support=0x{support:02X}")
            return (ic, ver, rev, support)
        return None

    def search_card(self) -> Optional[bytes]:
        """
        Tìm kiếm thẻ NFC (ISO14443A) trong vùng đọc.
        Trả về UID của thẻ nếu tìm thấy.
        """
        print("[*] Dang tim the...")

        # InListPassiveTarget: MaxTg=1, BrTy=0x00 (106 kbps type A)
        result = self._send_command(PN532_CMD_INLISTPASSIVETARGET, bytes([0x01, 0x00]))

        if result and len(result) >= 1:
            num_targets = result[0]
            if num_targets > 0 and len(result) >= 6:
                tg = result[1]
                sens_res = result[2:4]
                sel_res = result[4]
                uid_length = result[5]

                if len(result) >= 6 + uid_length:
                    uid = result[6:6 + uid_length]
                    self.uid = uid
                    self.uid_length = uid_length

                    print(f"[+] Tim thay the!")
                    print(f"    UID ({uid_length} bytes): {uid.hex(' ').upper()}")
                    print(f"    ATQA: {sens_res.hex(' ').upper()}")
                    print(f"    SAK: 0x{sel_res:02X}")

                    # Xác định loại thẻ
                    card_type = self._identify_card(sel_res)
                    print(f"    Loai the: {card_type}")
                    return uid

        print("[-] Khong tim thay the. Dat the len dau doc va thu lai.")
        return None

    def _identify_card(self, sak: int) -> str:
        """Xác định loại thẻ dựa trên SAK byte."""
        if sak == 0x08:
            return "MIFARE Classic 1K"
        elif sak == 0x18:
            return "MIFARE Classic 4K"
        elif sak == 0x00:
            return "MIFARE Ultralight / NTAG"
        elif sak == 0x09:
            return "MIFARE Mini"
        elif sak == 0x20:
            return "MIFARE Plus / ISO 14443-4"
        elif sak == 0x28:
            return "Smart MX with MIFARE Classic 1K"
        elif sak == 0x38:
            return "Smart MX with MIFARE Classic 4K"
        else:
            return f"Unknown (SAK=0x{sak:02X})"

    def authenticate(self, block: int, key: bytes = DEFAULT_KEY, key_type: str = "A") -> bool:
        """
        Xác thực với thẻ MIFARE Classic.
        block: số block (0-63 cho 1K)
        key: key 6 bytes
        key_type: 'A' hoặc 'B'
        """
        if self.uid is None:
            print("[-] Chua tim thay the. Goi search_card() truoc.")
            return False

        auth_cmd = MIFARE_CMD_AUTH_A if key_type == "A" else MIFARE_CMD_AUTH_B
        print(f"[*] Xac thuc block {block} voi Key {key_type}...")

        # InDataExchange: Tg=1, AUTH_CMD, Block, Key(6), UID(4)
        params = bytes([0x01, auth_cmd, block]) + key + self.uid[:4]
        result = self._send_command(PN532_CMD_INDATAEXCHANGE, params)

        if result and len(result) >= 1:
            if result[0] == 0x00:
                print("[+] Xac thuc thanh cong")
                return True

        print("[-] Xac thuc that bai")
        return False

    def read_block(self, block: int) -> Optional[bytes]:
        """Đọc 1 block (16 bytes) từ thẻ MIFARE Classic."""
        print(f"[*] Doc block {block}...")

        # InDataExchange: Tg=1, READ(0x30), Block
        params = bytes([0x01, MIFARE_CMD_READ, block])
        result = self._send_command(PN532_CMD_INDATAEXCHANGE, params)

        if result and len(result) >= 17:
            if result[0] == 0x00:
                block_data = result[1:17]
                print(f"[+] Block {block:02d}: {block_data.hex(' ').upper()}")
                # Hiển thị dạng ASCII
                ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in block_data)
                print(f"             ASCII: {ascii_str}")
                return block_data

        print(f"[-] Doc block {block} that bai")
        return None

    def write_block(self, block: int, data: bytes) -> bool:
        """
        Ghi 16 bytes vào 1 block trên thẻ MIFARE Classic.
        Cần authenticate trước.
        """
        if len(data) != 16:
            print("[-] Data phai dung 16 bytes")
            return False

        if block == 0:
            print("[-] CANH BAO: Khong nen ghi vao Block 0 (manufacturer block)!")
            return False

        if (block + 1) % 4 == 0:
            print(f"[!] CANH BAO: Block {block} la sector trailer!")
            print(f"    Ghi sai se khoa vinh vien sector nay!")
            confirm = input("    Ban co chac muon tiep tuc? (y/N): ")
            if confirm.lower() != "y":
                return False

        print(f"[*] Ghi vao block {block}...")

        # InDataExchange: Tg=1, WRITE(0xA0), Block, Data(16)
        params = bytes([0x01, MIFARE_CMD_WRITE, block]) + data
        result = self._send_command(PN532_CMD_INDATAEXCHANGE, params)

        if result and len(result) >= 1:
            if result[0] == 0x00:
                print(f"[+] Ghi block {block} thanh cong!")
                return True

        print(f"[-] Ghi block {block} that bai")
        return False

    def read_sector(self, sector: int, key: bytes = DEFAULT_KEY, key_type: str = "A") -> Optional[list]:
        """Đọc toàn bộ 1 sector (4 blocks)."""
        base_block = sector * 4
        print(f"\n{'='*55}")
        print(f"  Sector {sector:02d} (blocks {base_block:02d}-{base_block+3:02d})")
        print(f"{'='*55}")

        if not self.authenticate(base_block, key, key_type):
            return None

        blocks = []
        for i in range(4):
            block_data = self.read_block(base_block + i)
            blocks.append(block_data)

        return blocks

    def dump_card(self, key: bytes = DEFAULT_KEY, key_type: str = "A") -> dict:
        """Dump toàn bộ nội dung thẻ MIFARE Classic 1K (16 sectors)."""
        print("\n" + "=" * 60)
        print("          DUMP THE MIFARE CLASSIC 1K")
        print("=" * 60)

        uid = self.search_card()
        if not uid:
            return {}

        result = {}
        for sector in range(16):
            # Mỗi sector cần search_card lại vì auth reset
            self.search_card()
            blocks = self.read_sector(sector, key, key_type)
            result[sector] = blocks

        print("\n" + "=" * 60)
        print("  Dump hoan tat!")
        print("=" * 60)
        return result

    def write_text_to_block(self, block: int, text: str, key: bytes = DEFAULT_KEY) -> bool:
        """
        Ghi text vào 1 block (padding với 0x00 nếu < 16 bytes).
        Tiện ích giúp ghi text đơn giản.
        """
        data = text.encode('utf-8')[:16].ljust(16, b'\x00')

        uid = self.search_card()
        if not uid:
            return False

        if not self.authenticate(block, key):
            return False

        return self.write_block(block, data)

    def read_text_from_block(self, block: int, key: bytes = DEFAULT_KEY) -> Optional[str]:
        """Đọc text từ 1 block."""
        uid = self.search_card()
        if not uid:
            return None

        if not self.authenticate(block, key):
            return None

        data = self.read_block(block)
        if data:
            # Loại bỏ các null byte
            return data.rstrip(b'\x00').decode('utf-8', errors='replace')
        return None

    # ===== NTAG / MIFARE Ultralight =====

    def ntag_read(self, page: int) -> Optional[bytes]:
        """
        Đọc từ thẻ NTAG/Ultralight bằng lệnh READ (0x30).
        Trả về 16 bytes (4 pages liên tiếp bắt đầu từ `page`).
        """
        params = bytes([0x01, NTAG_CMD_READ, page])
        result = self._send_command(PN532_CMD_INDATAEXCHANGE, params)
        if result and len(result) >= 17 and result[0] == 0x00:
            return result[1:17]
        return None

    def ntag_write_page(self, page: int, data: bytes) -> bool:
        """
        Ghi 4 bytes vào 1 page của thẻ NTAG/Ultralight bằng lệnh WRITE (0xA2).
        """
        if len(data) != 4:
            print("[-] Du lieu page phai dung 4 bytes")
            return False

        params = bytes([0x01, NTAG_CMD_WRITE, page]) + data
        result = self._send_command(PN532_CMD_INDATAEXCHANGE, params)
        if result and len(result) >= 1 and result[0] == 0x00:
            return True
        return False

    def _build_ndef_uri(self, uri: str) -> bytes:
        """
        Tạo TLV chứa 1 NDEF record kiểu URI từ chuỗi `uri`.
        Trả về bytes sẵn sàng ghi vào vùng dữ liệu người dùng (bắt đầu page 4).
        """
        # Chọn prefix code dài nhất khớp với uri để rút gọn
        prefix_code = 0x00
        prefix_str = ""
        for code, pfx in NDEF_URI_PREFIX.items():
            if pfx and uri.startswith(pfx) and len(pfx) > len(prefix_str):
                prefix_code = code
                prefix_str = pfx

        uri_body = uri[len(prefix_str):].encode("utf-8")
        payload = bytes([prefix_code]) + uri_body  # payload của URI record

        # NDEF record: MB=1, ME=1, SR=1, TNF=0x01 (Well Known) -> 0xD1
        #   Type Length = 1, Payload Length = len(payload), Type = 'U' (0x55)
        record = bytes([0xD1, 0x01, len(payload), 0x55]) + payload

        # NDEF Message TLV: Tag=0x03, Length, Value=record ; Terminator TLV=0xFE
        tlv = bytes([0x03, len(record)]) + record + bytes([0xFE])
        return tlv

    def write_url_ntag(self, url: str) -> bool:
        """
        Ghi 1 URL vào thẻ NTAG/Ultralight dưới dạng NDEF.
        Dữ liệu người dùng bắt đầu từ page 4.
        """
        uid = self.search_card()
        if not uid:
            return False

        tlv = self._build_ndef_uri(url)
        # Chia thành các page 4 bytes (pad 0x00)
        pages = [tlv[i:i + 4].ljust(4, b"\x00") for i in range(0, len(tlv), 4)]

        print(f"[*] Ghi NDEF URI '{url}' ({len(tlv)} bytes -> {len(pages)} pages)...")
        start_page = 4
        for idx, page_data in enumerate(pages):
            page_no = start_page + idx
            if not self.ntag_write_page(page_no, page_data):
                print(f"[-] Ghi page {page_no} that bai")
                return False
            print(f"[+] Page {page_no:02d}: {page_data.hex(' ').upper()}")

        print(f"[+] Ghi URL thanh cong!")
        return True

    def read_url_ntag(self) -> Optional[str]:
        """Đọc và giải mã NDEF URI từ thẻ NTAG/Ultralight."""
        uid = self.search_card()
        if not uid:
            return None

        # Đọc từ page 4 trở đi, gom dữ liệu
        raw = bytearray()
        for page in range(4, 40, 4):  # đọc từng khối 4 page cho đến khi gặp terminator
            chunk = self.ntag_read(page)
            if not chunk:
                break
            raw += chunk
            if 0xFE in chunk:  # terminator TLV
                break

        # Phân tích TLV tìm NDEF message (tag 0x03)
        i = 0
        while i < len(raw):
            tag = raw[i]
            if tag == 0x00:  # NULL TLV
                i += 1
                continue
            if tag == 0xFE:  # Terminator
                break
            if i + 1 >= len(raw):
                break
            length = raw[i + 1]
            value = raw[i + 2:i + 2 + length]
            if tag == 0x03:  # NDEF Message
                return self._parse_ndef_uri(bytes(value))
            i += 2 + length
        return None

    def _parse_ndef_uri(self, record: bytes) -> Optional[str]:
        """Giải mã 1 NDEF URI record thành chuỗi URL."""
        if len(record) < 4:
            return None
        # header, type_len, payload_len, type_byte
        type_len = record[1]
        payload_len = record[2]
        type_byte = record[3]
        payload = record[4:4 + payload_len]
        if type_len == 1 and type_byte == 0x55 and payload:  # 'U'
            prefix = NDEF_URI_PREFIX.get(payload[0], "")
            return prefix + payload[1:].decode("utf-8", errors="replace")
        return None


def list_nfc_ports() -> list:
    """
    Liệt kê các cổng COM có khả năng là đầu đọc NFC-PM5 (PN532 qua USB-Serial).
    Trả về list các tuple (device, description).
    Ưu tiên các cổng có chip USB-Serial phổ biến (CH340/CH341/CP210x/FTDI/serial).
    Nếu không có cổng nào khớp từ khóa, trả về TẤT CẢ các cổng COM hiện có
    (để người dùng tự chọn), vì một số driver đặt tên khác nhau.
    """
    ports = serial.tools.list_ports.comports()
    keywords = ("ch340", "ch341", "cp210", "ftdi", "serial", "usb-serial", "uart")

    matched = []
    for p in ports:
        desc = (p.description or "").lower()
        if any(k in desc for k in keywords):
            matched.append((p.device, p.description))

    if matched:
        return matched
    # Dự phòng: trả về tất cả cổng COM để người dùng tự chọn
    return [(p.device, p.description) for p in ports]


def detect_port() -> Optional[str]:
    """Tự động phát hiện cổng COM của NFC-PM5."""
    ports = serial.tools.list_ports.comports()
    for port in ports:
        desc = port.description.lower()
        if "ch340" in desc or "ch341" in desc or "serial" in desc:
            print(f"[+] Phat hien: {port.device} - {port.description}")
            return port.device

    if ports:
        print("[*] Cac cong COM hien co:")
        for port in ports:
            print(f"    {port.device}: {port.description}")
        return ports[0].device

    print("[-] Khong tim thay cong COM nao")
    return None




# =====================================================================
#  I18N - Đa ngôn ngữ (bản dịch nằm ở các file JSON trong thư mục lang/)
# =====================================================================
import json as _json

def _app_dir() -> str:
    """Thư mục chứa app (hỗ trợ cả khi đóng gói PyInstaller)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

_LANG_DIR = os.path.join(_app_dir(), "lang")
_CONFIG_PATH = os.path.join(_app_dir(), "config.json")

# Danh sách ngôn ngữ hỗ trợ: (mã, tên hiển thị)
LANGUAGES = [
    ("vi", "Tiếng Việt"),
    ("en", "English"),
    ("ja", "日本語"),
]

_DEFAULT_LANG = "en"
_current_lang = _DEFAULT_LANG
_translations: dict = {}   # cache: {lang_code: {key: text}}


def _load_lang(lang: str) -> dict:
    """Đọc file JSON ngôn ngữ lang/<lang>.json. Cache lại."""
    if lang in _translations:
        return _translations[lang]
    path = os.path.join(_LANG_DIR, f"{lang}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
    except Exception:
        data = {}
    _translations[lang] = data
    return data


def tr(key: str, **kwargs) -> str:
    """Lấy chuỗi dịch theo ngôn ngữ hiện tại. Hỗ trợ format {tên}."""
    data = _load_lang(_current_lang)
    text = data.get(key)
    if text is None:
        # fallback về ngôn ngữ mặc định rồi về chính key
        text = _load_lang(_DEFAULT_LANG).get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text


def set_language(lang: str):
    """Đặt ngôn ngữ hiện tại (không tự lưu)."""
    global _current_lang
    if any(code == lang for code, _ in LANGUAGES):
        _current_lang = lang


def get_language() -> str:
    return _current_lang


def language_name(code: str) -> str:
    for c, name in LANGUAGES:
        if c == code:
            return name
    return code


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_config(data: dict):
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_saved_language() -> Optional[str]:
    lang = _load_config().get("language")
    return lang if lang in [c for c, _ in LANGUAGES] else None


def save_language(lang: str):
    cfg = _load_config()
    cfg["language"] = lang
    _save_config(cfg)
    set_language(lang)



# ===== Chế độ hoạt động của worker =====
class Mode(Enum):
    IDLE = "idle"
    INPUT = "input"    # gõ nội dung thẻ vào ô focus
    READ = "read"      # đọc liên tục, hiển thị lên GUI
    WRITE = "write"    # ghi liên tục: mỗi thẻ mới chạm là ghi


POLL_INTERVAL = 0.2         # giây giữa mỗi lần poll
ABSENCE_TO_RESET = 0.6      # vắng thẻ bao lâu thì coi là đã nhấc thẻ ra


class NFCWorker(QThread):
    """
    Worker thread độc quyền quản lý PN532.
    Chỉ 1 chế độ tại 1 thời điểm -> đảm bảo input & read/write loại trừ nhau.
    """
    # Signals -> GUI
    log = pyqtSignal(str)
    connected = pyqtSignal(bool, str)       # (ok, message)
    card_read = pyqtSignal(str, str)        # (uid_hex, ndef_or_empty)
    typed = pyqtSignal(str, str)            # (uid_hex, text_typed)
    write_result = pyqtSignal(bool, str)    # (ok, message)
    mode_changed = pyqtSignal(str)          # tên mode hiện tại
    line_progress = pyqtSignal(int, int, str)  # (index dòng, tổng số dòng, "writing"|"done"|"finished")

    def __init__(self, port: str):
        super().__init__()
        self.port = port
        self._mode = Mode.IDLE
        self._running = True
        self.reader: Optional[NFCPM5] = None


        # cấu hình input
        self.input_source = "auto"   # auto | ndef | uid
        self.input_enter = False
        self.uid_sep = ""
        # Chuỗi phím tự động nhấn SAU khi gõ nội dung (theo thứ tự).
        # Mỗi phần tử là tên phím của thư viện keyboard: tab, space, enter,
        # up, down, left, right.
        self.input_postkeys: list = []

        # hàng chờ lệnh ghi: đặt url vào đây để yêu cầu ghi (one-shot)
        self._write_queue: "queue.Queue[str]" = queue.Queue()

        # nội dung dùng cho CHẾ ĐỘ GHI LIÊN TỤC (Mode.WRITE)
        self._write_text: str = ""

        # Nguồn ghi từ FILE TXT (nếu có): mỗi dòng 1 nội dung, ghi lần lượt.
        self._write_lines: Optional[list] = None   # None = dùng _write_text (ô nhập)
        self._write_index: int = 0                  # con trỏ dòng hiện tại

        # trạng thái chống lặp khi giữ thẻ
        self._last_uid: Optional[str] = None
        self._last_seen = 0.0

    # ---- API gọi từ GUI thread (thread-safe ở mức gần đúng cho GUI đơn) ----
    def set_mode(self, mode: Mode):
        self._mode = mode
        self._last_uid = None
        self.mode_changed.emit(mode.value)
        self.log.emit(tr("log_mode_changed", mode=mode.value.upper()))

    def request_write(self, url: str):
        """Yêu cầu ghi URL (sẽ tự động chuyển sang trạng thái ghi tạm thời)."""
        self._write_queue.put(url)

    def set_input_config(self, source: str, enter: bool, uid_sep: str, postkeys: list = None):
        self.input_source = source
        self.input_enter = enter
        self.uid_sep = uid_sep
        self.input_postkeys = postkeys or []

    def set_write_text(self, text: str):
        """Đặt nội dung cho chế độ ghi liên tục (Mode.WRITE) - nguồn từ ô nhập."""
        self._write_text = text

    def set_write_lines(self, lines: Optional[list]):
        """
        Đặt nguồn ghi từ FILE TXT (danh sách dòng). Đặt None để quay về ô nhập.
        Reset con trỏ về đầu.
        """
        self._write_lines = lines
        self._write_index = 0

    def stop(self):
        self._running = False

    # ---- Vòng đời thread ----
    def run(self):
        port = self.port
        if not port:
            self.connected.emit(False, tr("conn_no_port"))
            return

        self.reader = NFCPM5(port=port)
        if not self.reader.connect():
            self.connected.emit(False, tr("conn_fail", port=port))
            return

        self.connected.emit(True, tr("conn_ok", port=port))
        self.log.emit(tr("log_worker_ready"))

        try:
            while self._running:
                # Ưu tiên xử lý lệnh ghi nếu có
                if not self._write_queue.empty():
                    url = self._write_queue.get()
                    self._do_write(url)
                    continue

                if self._mode == Mode.INPUT:
                    self._poll_input()
                elif self._mode == Mode.READ:
                    self._poll_read()
                elif self._mode == Mode.WRITE:
                    self._poll_write()
                else:
                    time.sleep(0.1)

                time.sleep(POLL_INTERVAL)
        except Exception as e:
            self.log.emit(tr("log_worker_error", err=str(e)))
        finally:
            if self.reader:
                self.reader.disconnect()
            self.log.emit(tr("log_worker_stopped"))

    # ---- Poll helpers ----
    def _poll_card_uid(self) -> Optional[bytes]:
        # Dùng poll_target với timeout ngắn -> vòng lặp phản hồi nhanh,
        # không block ~2s mỗi lần chưa có thẻ (quan trọng cho đọc/ghi liên tục).
        return self.reader.poll_target(timeout=0.15)

    def _is_new_tap(self, uid: bytes) -> bool:
        """True nếu đây là lần chạm mới (chống lặp khi giữ thẻ)."""
        now = time.time()
        uid_hex = uid.hex()
        if uid_hex != self._last_uid:
            self._last_uid = uid_hex
            self._last_seen = now
            return True
        self._last_seen = now
        return False

    def _tick_absence(self):
        """Reset trạng thái khi thẻ đã được nhấc ra đủ lâu."""
        if self._last_uid is not None and (time.time() - self._last_seen) >= ABSENCE_TO_RESET:
            self._last_uid = None

    def _build_input_text(self, uid: bytes) -> str:
        uid_str = self.uid_sep.join(f"{b:02X}" for b in uid)
        if self.input_source == "uid":
            return uid_str
        url = None
        try:
            url = self.reader.read_url_ntag()
        except Exception:
            url = None
        if url:
            return url
        if self.input_source == "ndef":
            return ""
        return uid_str  # auto fallback

    def _poll_input(self):
        uid = self._poll_card_uid()
        if uid is None:
            self._tick_absence()
            return
        if self._is_new_tap(uid):
            text = self._build_input_text(uid)
            if text:
                # Gõ nội dung qua lớp trừu tượng đa nền tảng (_typer):
                # Windows dùng `keyboard`, Linux/macOS dùng `pynput`.
                _typer.type_text(text)
                # Nhấn chuỗi phím tự động (theo thứ tự) sau khi gõ nội dung
                for key in self.input_postkeys:
                    try:
                        _typer.press_key(key)
                        time.sleep(0.02)
                    except Exception:
                        pass
                # Tương thích ngược: nếu bật checkbox Enter riêng (và chưa có trong postkeys)
                if self.input_enter and "enter" not in self.input_postkeys:
                    _typer.press_key("enter")
                self.typed.emit(uid.hex(" ").upper(), text)
            else:
                self.log.emit(tr("log_no_ndef_skip", uid=uid.hex(' ').upper()))

    def _poll_read(self):
        uid = self._poll_card_uid()
        if uid is None:
            self._tick_absence()
            return
        if self._is_new_tap(uid):
            ndef = ""
            try:
                ndef = self.reader.read_url_ntag() or ""
            except Exception:
                ndef = ""
            self.card_read.emit(uid.hex(" ").upper(), ndef)

    def _write_and_verify(self, url: str) -> bool:
        """Ghi url vào thẻ đang ở trên đầu đọc + đọc lại xác minh. Phát write_result."""
        try:
            ok = self.reader.write_url_ntag(url)
        except Exception as e:
            self.write_result.emit(False, tr("wr_error", err=str(e)))
            return False
        if not ok:
            self.write_result.emit(False, tr("wr_fail"))
            return False
        # đọc lại xác minh
        verify = None
        try:
            verify = self.reader.read_url_ntag()
        except Exception:
            verify = None
        if verify == url:
            self.write_result.emit(True, tr("wr_ok_verified", url=url))
        else:
            self.write_result.emit(True, tr("wr_ok_unverified", verify=str(verify)))
        return True

    def _poll_write(self):
        """
        CHẾ ĐỘ GHI LIÊN TỤC. Hai nguồn:
          - FILE TXT (_write_lines != None): ghi lần lượt mỗi dòng 1 thẻ. Chạm
            thẻ mới -> ghi dòng con trỏ hiện tại, thành công thì tiến con trỏ.
            Hết dòng -> tự dừng.
          - Ô NHẬP (_write_lines == None): ghi _write_text cho mỗi thẻ mới (như cũ).
        """
        # Xác định nội dung sẽ ghi cho lần chạm kế tiếp
        if self._write_lines is not None:
            if self._write_index >= len(self._write_lines):
                # Đã ghi hết tất cả các dòng -> dừng
                self.line_progress.emit(self._write_index, len(self._write_lines), "finished")
                self.set_mode(Mode.IDLE)
                return
            content = self._write_lines[self._write_index]
        else:
            content = self._write_text

        if not content:
            time.sleep(0.1)
            return

        uid = self._poll_card_uid()
        if uid is None:
            self._tick_absence()
            return

        if self._is_new_tap(uid):
            self.log.emit(tr("wr_detected", uid=uid.hex(" ").upper()))
            ok = self._write_and_verify(content)
            if ok and self._write_lines is not None:
                # Đánh dấu dòng vừa ghi xong, tiến con trỏ
                self.line_progress.emit(self._write_index, len(self._write_lines), "done")
                self._write_index += 1
                if self._write_index < len(self._write_lines):
                    self.line_progress.emit(self._write_index, len(self._write_lines), "writing")
                else:
                    self.line_progress.emit(self._write_index, len(self._write_lines), "finished")
                    self.set_mode(Mode.IDLE)

    def _do_write(self, url: str):
        """One-shot: chờ chạm 1 thẻ và ghi URL. Timeout để không treo."""
        self.log.emit(tr("wr_waiting", url=url))
        deadline = time.time() + 20
        wrote = False
        self._last_uid = None
        while time.time() < deadline and self._running:
            uid = self._poll_card_uid()
            if uid is not None:
                self._write_and_verify(url)
                wrote = True
                break
            time.sleep(0.15)
        if not wrote:
            self.write_result.emit(False, tr("wr_timeout"))


_icon_cache = None

def make_icon(color: str = "#2d7dd2") -> QIcon:
    """
    Icon của app/tray. Ưu tiên nạp file icon.ico / icon.png đặt cạnh app
    (hoặc trong bundle PyInstaller); nếu không có thì vẽ runtime bằng QPainter.
    """
    global _icon_cache
    if _icon_cache is not None:
        return _icon_cache

    # Thử nạp file icon cạnh app (_app_dir xử lý cả khi đóng gói PyInstaller)
    for name in ("icon.ico", "icon.png"):
        path = os.path.join(_app_dir(), name)
        if os.path.exists(path):
            ic = QIcon(path)
            if not ic.isNull():
                _icon_cache = ic
                return ic

    # Fallback: vẽ icon tròn đơn giản
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(6, 6, 52, 52)
    p.setPen(QColor("white"))
    f = QFont("Arial", 22, QFont.Bold)
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignCenter, "NFC")
    p.end()
    _icon_cache = QIcon(pix)
    return _icon_cache


class MainWindow(QMainWindow):
    def __init__(self, port: str):
        super().__init__()
        self.port = port
        self.setWindowTitle(f"{tr('app_title')}  [{port}]")
        self.setWindowIcon(make_icon())
        self.resize(560, 540)

        self._quit_requested = False

        # Worker: tao va ket noi signal qua _start_worker (dung lai khi refresh)
        self.worker = None
        self._start_worker(port)

        self._build_ui()
        self._build_tray()

    def _start_worker(self, port: str):
        """Tao worker moi cho `port` va ket noi cac signal, roi chay."""
        self.worker = NFCWorker(port)
        self.worker.log.connect(self.append_log)
        self.worker.connected.connect(self.on_connected)
        self.worker.card_read.connect(self.on_card_read)
        self.worker.typed.connect(self.on_typed)
        self.worker.write_result.connect(self.on_write_result)
        self.worker.mode_changed.connect(self.on_mode_changed)
        self.worker.line_progress.connect(self.on_line_progress)
        self.worker.start()

    # ---------------- UI ----------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Thanh trạng thái kết nối + chế độ
        status_row = QHBoxLayout()
        self.lbl_conn = QLabel(tr("connecting"))
        self.lbl_conn.setStyleSheet("font-weight:bold;")
        self.lbl_mode = QLabel(tr("mode_label", mode="IDLE"))
        self.lbl_mode.setStyleSheet("color:#555;")
        status_row.addWidget(self.lbl_conn)
        status_row.addStretch()
        status_row.addWidget(self.lbl_mode)
        # Chọn ngôn ngữ ngay trên giao diện
        self.cbo_lang = QComboBox()
        for code, name in LANGUAGES:
            self.cbo_lang.addItem(name, code)
        # chọn đúng ngôn ngữ hiện tại
        cur = get_language()
        for i in range(self.cbo_lang.count()):
            if self.cbo_lang.itemData(i) == cur:
                self.cbo_lang.setCurrentIndex(i)
                break
        self.cbo_lang.currentIndexChanged.connect(self._on_lang_combo_changed)
        status_row.addWidget(self.cbo_lang)
        # Nút làm mới / kiểm tra lại thiết bị
        self.btn_refresh = QPushButton(tr("btn_refresh"))
        self.btn_refresh.clicked.connect(self._refresh_device)
        status_row.addWidget(self.btn_refresh)
        # Nút thoát app ngay trên giao diện
        self.btn_quit = QPushButton(tr("tray_quit"))
        self.btn_quit.clicked.connect(self._real_quit)
        status_row.addWidget(self.btn_quit)
        root.addLayout(status_row)

        # ===== Thanh CHẾ ĐỘ CHUNG (luôn hiện) - chỉ 1 chế độ active =====
        # Dùng QButtonGroup exclusive để đảm bảo chỉ 1 nút sáng.
        self.mode_box = QGroupBox(tr("mode_bar_title"))
        mode_lay = QHBoxLayout(self.mode_box)
        self.btn_input = QPushButton(tr("mode_input"))
        self.btn_read = QPushButton(tr("mode_read"))
        self.btn_write_cont = QPushButton(tr("mode_write"))
        for b in (self.btn_input, self.btn_read, self.btn_write_cont):
            b.setCheckable(True)
            b.setMinimumHeight(36)
            mode_lay.addWidget(b)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(False)  # tự quản lý để cho phép bỏ chọn hết
        self.mode_group.addButton(self.btn_input)
        self.mode_group.addButton(self.btn_read)
        self.mode_group.addButton(self.btn_write_cont)
        self.btn_input.clicked.connect(self._toggle_input)
        self.btn_read.clicked.connect(self._toggle_read)
        self.btn_write_cont.clicked.connect(self._toggle_write_cont)
        root.addWidget(self.mode_box)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._tab_input(), tr("tab_input"))
        self.tabs.addTab(self._tab_read(), tr("tab_read"))
        self.tabs.addTab(self._tab_write(), tr("tab_write"))
        self.tabs.addTab(self._tab_log(), tr("tab_log"))
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Index của tab Log (để loại trừ mode xử lý đúng)
        self.LOG_TAB_INDEX = 3

    def _tab_log(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        lay.addWidget(self.txt_log, 1)
        return w

    def _tab_input(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_input_info = QLabel(tr("input_info"))
        self.lbl_input_info.setWordWrap(True)
        lay.addWidget(self.lbl_input_info)

        self.box_input_src = QGroupBox(tr("input_source_box"))
        bl = QVBoxLayout(self.box_input_src)
        self.rb_auto = QRadioButton(tr("src_auto"))
        self.rb_ndef = QRadioButton(tr("src_ndef"))
        self.rb_uid = QRadioButton(tr("src_uid"))
        self.rb_auto.setChecked(True)
        for rb in (self.rb_auto, self.rb_ndef, self.rb_uid):
            bl.addWidget(rb)
            rb.toggled.connect(self._apply_input_config)
        lay.addWidget(self.box_input_src)

        self.chk_enter = QCheckBox(tr("chk_enter"))
        self.chk_enter.toggled.connect(self._apply_input_config)
        lay.addWidget(self.chk_enter)

        # ===== Phím tự động nhấn SAU khi gõ (có thứ tự, có thể kết hợp) =====
        self.box_postkeys = QGroupBox(tr("postkeys_box"))
        pk = QVBoxLayout(self.box_postkeys)
        self.lbl_postkeys_help = QLabel(tr("postkeys_help"))
        self.lbl_postkeys_help.setWordWrap(True)
        self.lbl_postkeys_help.setStyleSheet("color:#666;")
        pk.addWidget(self.lbl_postkeys_help)

        # Hàng nút chèn nhanh: mỗi nút thêm 1 phím vào chuỗi (theo thứ tự bấm)
        self._postkeys = []   # danh sách tên phím keyboard theo thứ tự
        self._KEY_DEFS = [    # (keyboard_name, nhãn hiển thị)
            ("tab", "Tab"), ("space", "Space"), ("enter", "Enter"),
            ("up", "↑"), ("down", "↓"), ("left", "←"), ("right", "→"),
        ]
        btn_row = QHBoxLayout()
        for kname, label in self._KEY_DEFS:
            b = QPushButton(label)
            b.setFixedWidth(54)
            b.clicked.connect(lambda _, k=kname: self._add_postkey(k))
            btn_row.addWidget(b)
        btn_row.addStretch()
        pk.addLayout(btn_row)

        # Ô hiển thị chuỗi phím hiện tại + nút xóa
        seq_row = QHBoxLayout()
        self.lbl_postkeys_seq = QLabel(tr("postkeys_empty"))
        self.lbl_postkeys_seq.setStyleSheet(
            "padding:6px; border:1px solid #ccc; border-radius:4px; background:#fafafa;")
        self.lbl_postkeys_seq.setWordWrap(True)
        seq_row.addWidget(self.lbl_postkeys_seq, 1)
        self.btn_postkeys_undo = QPushButton(tr("postkeys_undo"))
        self.btn_postkeys_undo.clicked.connect(self._undo_postkey)
        self.btn_postkeys_clear = QPushButton(tr("postkeys_clear"))
        self.btn_postkeys_clear.clicked.connect(self._clear_postkeys)
        seq_row.addWidget(self.btn_postkeys_undo)
        seq_row.addWidget(self.btn_postkeys_clear)
        pk.addLayout(seq_row)
        lay.addWidget(self.box_postkeys)

        lay.addStretch()
        return w

    # ---------- Chuỗi phím tự động (post-keys) ----------
    def _postkey_label(self, kname: str) -> str:
        for k, lbl in self._KEY_DEFS:
            if k == kname:
                return lbl
        return kname

    def _refresh_postkeys_view(self):
        if self._postkeys:
            self.lbl_postkeys_seq.setText(
                "  →  ".join(self._postkey_label(k) for k in self._postkeys))
        else:
            self.lbl_postkeys_seq.setText(tr("postkeys_empty"))

    def _add_postkey(self, kname: str):
        self._postkeys.append(kname)
        self._refresh_postkeys_view()
        self._apply_input_config()

    def _undo_postkey(self):
        if self._postkeys:
            self._postkeys.pop()
            self._refresh_postkeys_view()
            self._apply_input_config()

    def _clear_postkeys(self):
        self._postkeys = []
        self._refresh_postkeys_view()
        self._apply_input_config()

    def _tab_read(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_read_info = QLabel(tr("read_info"))
        self.lbl_read_info.setWordWrap(True)
        lay.addWidget(self.lbl_read_info)

        self.lbl_uid = QLabel(tr("lbl_uid"))
        lay.addWidget(self.lbl_uid)
        self.out_uid = QLineEdit(); self.out_uid.setReadOnly(True)
        lay.addWidget(self.out_uid)

        self.lbl_ndef = QLabel(tr("lbl_ndef"))
        lay.addWidget(self.lbl_ndef)
        self.out_ndef = QLineEdit(); self.out_ndef.setReadOnly(True)
        lay.addWidget(self.out_ndef)

        lay.addStretch()
        return w

    def _tab_write(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_write_info = QLabel(tr("write_info"))
        self.lbl_write_info.setWordWrap(True)
        lay.addWidget(self.lbl_write_info)

        self.lbl_write_content = QLabel(tr("lbl_write_content"))
        lay.addWidget(self.lbl_write_content)
        self.in_write = QLineEdit()
        self.in_write.setPlaceholderText("vd: https://132.one")
        lay.addWidget(self.in_write)

        # ===== Nguồn ghi từ FILE TXT (tùy chọn) =====
        file_box = QGroupBox(tr("file_box_title"))
        self.file_box = file_box
        fb = QVBoxLayout(file_box)
        file_row = QHBoxLayout()
        self.btn_choose_file = QPushButton(tr("btn_choose_file"))
        self.btn_choose_file.clicked.connect(self._choose_write_file)
        self.btn_clear_file = QPushButton(tr("btn_clear_file"))
        self.btn_clear_file.clicked.connect(self._clear_write_file)
        self.btn_clear_file.setEnabled(False)
        file_row.addWidget(self.btn_choose_file)
        file_row.addWidget(self.btn_clear_file)
        fb.addLayout(file_row)
        self.lbl_file_name = QLabel(tr("file_none"))
        self.lbl_file_name.setStyleSheet("color:#555;")
        fb.addWidget(self.lbl_file_name)
        # Danh sách dòng + trạng thái tiến trình
        self.list_lines = QListWidget()
        self.list_lines.setMaximumHeight(140)
        self.list_lines.setVisible(False)
        fb.addWidget(self.list_lines)
        self.lbl_file_progress = QLabel("")
        self.lbl_file_progress.setVisible(False)
        fb.addWidget(self.lbl_file_progress)
        lay.addWidget(file_box)

        self._write_lines = []        # nội dung các dòng (GUI side)
        self._write_file_path = None  # None = dùng ô nhập

        # Nút ghi 1 lần (one-shot)
        self.btn_write = QPushButton(tr("btn_write"))
        self.btn_write.clicked.connect(self._do_write_clicked)
        lay.addWidget(self.btn_write)

        # Dòng trạng thái CHẾ ĐỘ (info/idle) - có thể thay đổi theo chế độ
        self.lbl_write_status = QLabel(tr("write_status_idle"))
        self.lbl_write_status.setWordWrap(True)
        self.lbl_write_status.setStyleSheet("padding:4px; color:#666;")
        lay.addWidget(self.lbl_write_status)

        # Bộ đếm số thẻ đã ghi ("Đã ghi: N thẻ") - đặt TRÊN dòng kết quả
        self._write_count = 0
        self.lbl_write_count = QLabel(tr("write_count", n=0))
        lay.addWidget(self.lbl_write_count)

        # Dòng KẾT QUẢ GHI ("✅ Ghi thành công...") - đặt DƯỚI bộ đếm,
        # tự động xóa sau 1 giây.
        self.lbl_write_result = QLabel("")
        self.lbl_write_result.setWordWrap(True)
        self.lbl_write_result.setStyleSheet("padding:8px; border-radius:4px; background:#eee;")
        self.lbl_write_result.setVisible(False)
        lay.addWidget(self.lbl_write_result)

        # Timer tự xóa dòng kết quả sau 1s
        self._write_result_timer = QTimer(self)
        self._write_result_timer.setSingleShot(True)
        self._write_result_timer.timeout.connect(self._clear_write_result)

        lay.addStretch()
        return w

    def _clear_write_result(self):
        self.lbl_write_result.clear()
        self.lbl_write_result.setVisible(False)

    # ---------- Ghi từ file txt ----------
    def _choose_write_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("btn_choose_file"), "", "Text files (*.txt);;All files (*.*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.readlines()]
        except Exception as e:
            QMessageBox.warning(self, tr("file_box_title"), str(e))
            return
        # bỏ các dòng rỗng
        lines = [ln for ln in lines if ln]
        if not lines:
            QMessageBox.warning(self, tr("file_box_title"), tr("file_empty"))
            return
        self._write_lines = lines
        self._write_file_path = path
        self.btn_clear_file.setEnabled(True)
        self.in_write.setEnabled(False)   # dùng file -> không dùng ô nhập
        self.lbl_file_name.setText(
            tr("file_loaded", name=os.path.basename(path), n=len(lines)))
        self._rebuild_lines_view(current=0)
        self.list_lines.setVisible(True)
        self.lbl_file_progress.setVisible(True)
        self.lbl_file_progress.setText(tr("file_progress", done=0, total=len(lines)))

    def _clear_write_file(self):
        self._write_lines = []
        self._write_file_path = None
        self.btn_clear_file.setEnabled(False)
        self.in_write.setEnabled(True)
        self.lbl_file_name.setText(tr("file_none"))
        self.list_lines.clear()
        self.list_lines.setVisible(False)
        self.lbl_file_progress.setVisible(False)

    def _rebuild_lines_view(self, current: int = 0, done_until: int = -1):
        """Dựng lại danh sách dòng với đánh dấu trạng thái."""
        self.list_lines.clear()
        for i, text in enumerate(self._write_lines):
            if i <= done_until:
                prefix = "✓ "
            elif i == current:
                prefix = "▶ "
            else:
                prefix = "   "
            item = QListWidgetItem(f"{prefix}{i+1}. {text}")
            if i <= done_until:
                item.setForeground(QColor("#1b6b1b"))
            elif i == current:
                item.setForeground(QColor("#31708f"))
            self.list_lines.addItem(item)
        # cuộn tới dòng hiện tại
        if 0 <= current < self.list_lines.count():
            self.list_lines.setCurrentRow(current)

    def on_line_progress(self, index: int, total: int, status: str):
        """Nhận tiến trình từ worker: cập nhật UI dòng đang/đã ghi."""
        if status == "writing":
            self._rebuild_lines_view(current=index, done_until=index - 1)
            self.lbl_file_progress.setText(tr("file_progress", done=index, total=total))
        elif status == "done":
            self._rebuild_lines_view(current=index + 1, done_until=index)
            self.lbl_file_progress.setText(tr("file_progress", done=index + 1, total=total))
        elif status == "finished":
            self._rebuild_lines_view(current=-1, done_until=total - 1)
            self.lbl_file_progress.setText(tr("file_done", total=total))
            # tự tắt nút ghi liên tục
            if self.btn_write_cont.isChecked():
                self.btn_write_cont.setChecked(False)
            self._set_write_status(tr("write_status_idle"), "idle")

    def _build_tray(self):
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip(tr("tray_tooltip"))
        self.tray_menu = QMenu()
        self.act_show = self.tray_menu.addAction(tr("tray_show"))
        self.act_show.triggered.connect(self._show_from_tray)

        # Submenu ngôn ngữ
        self.lang_menu = self.tray_menu.addMenu(tr("tray_language"))
        self._lang_actions = []
        for code, name in LANGUAGES:
            act = self.lang_menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(code == get_language())
            act.triggered.connect(lambda _, c=code: self._change_language(c))
            self._lang_actions.append((code, act))

        self.tray_menu.addSeparator()
        self.act_quit = self.tray_menu.addAction(tr("tray_quit"))
        self.act_quit.triggered.connect(self._real_quit)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _change_language(self, code: str):
        save_language(code)
        for c, act in self._lang_actions:
            act.setChecked(c == code)
        # Đồng bộ combobox ngôn ngữ (chặn tín hiệu để tránh đệ quy)
        if hasattr(self, "cbo_lang"):
            self.cbo_lang.blockSignals(True)
            for i in range(self.cbo_lang.count()):
                if self.cbo_lang.itemData(i) == code:
                    self.cbo_lang.setCurrentIndex(i)
                    break
            self.cbo_lang.blockSignals(False)
        self.retranslate()

    def _on_lang_combo_changed(self, index: int):
        """Đổi ngôn ngữ khi người dùng chọn trên combobox giao diện."""
        code = self.cbo_lang.itemData(index)
        if code and code != get_language():
            self._change_language(code)

    def retranslate(self):
        """Cập nhật lại toàn bộ chuỗi hiển thị theo ngôn ngữ hiện tại."""
        self.setWindowTitle(f"{tr('app_title')}  [{self.port}]")
        self.lbl_mode.setText(tr("mode_label", mode=self.worker._mode.value.upper()))
        # Thanh chế độ chung
        self.mode_box.setTitle(tr("mode_bar_title"))
        self.btn_input.setText(tr("mode_input"))
        self.btn_read.setText(tr("mode_read"))
        self.btn_write_cont.setText(tr("mode_write"))
        # Tabs
        self.tabs.setTabText(0, tr("tab_input"))
        self.tabs.setTabText(1, tr("tab_read"))
        self.tabs.setTabText(2, tr("tab_write"))
        self.tabs.setTabText(3, tr("tab_log"))
        # Tab input
        self.lbl_input_info.setText(tr("input_info"))
        self.box_input_src.setTitle(tr("input_source_box"))
        self.rb_auto.setText(tr("src_auto"))
        self.rb_ndef.setText(tr("src_ndef"))
        self.rb_uid.setText(tr("src_uid"))
        self.chk_enter.setText(tr("chk_enter"))
        self.box_postkeys.setTitle(tr("postkeys_box"))
        self.lbl_postkeys_help.setText(tr("postkeys_help"))
        self.btn_postkeys_undo.setText(tr("postkeys_undo"))
        self.btn_postkeys_clear.setText(tr("postkeys_clear"))
        self._refresh_postkeys_view()
        # Tab read
        self.lbl_read_info.setText(tr("read_info"))
        self.lbl_uid.setText(tr("lbl_uid"))
        self.lbl_ndef.setText(tr("lbl_ndef"))
        # Tab write
        self.lbl_write_info.setText(tr("write_info"))
        self.lbl_write_content.setText(tr("lbl_write_content"))
        self.btn_write.setText(tr("btn_write"))
        self.lbl_write_count.setText(tr("write_count", n=self._write_count))
        self.btn_choose_file.setText(tr("btn_choose_file"))
        self.btn_clear_file.setText(tr("btn_clear_file"))
        self.file_box.setTitle(tr("file_box_title"))
        if not self._write_file_path:
            self.lbl_file_name.setText(tr("file_none"))
        # Tray
        self.tray.setToolTip(tr("tray_tooltip"))
        self.act_show.setText(tr("tray_show"))
        self.lang_menu.setTitle(tr("tray_language"))
        self.act_quit.setText(tr("tray_quit"))
        self.btn_quit.setText(tr("tray_quit"))
        self.btn_refresh.setText(tr("btn_refresh"))

    # ---------------- Logic chuyển mode ----------------
    def _turn_off_input(self):
        if self.btn_input.isChecked():
            self.btn_input.setChecked(False)

    def _turn_off_read(self):
        if self.btn_read.isChecked():
            self.btn_read.setChecked(False)

    def _turn_off_write_cont(self):
        if self.btn_write_cont.isChecked():
            self.btn_write_cont.setChecked(False)
        self._set_write_status(tr("write_status_idle"), "idle")

    def _on_tab_changed(self, index: int):
        """
        Đổi tab KHÔNG làm thay đổi chế độ đang chạy. Chế độ (Input/Read/Write)
        được điều khiển qua thanh chế độ chung ở trên, loại trừ nhau. Người dùng
        có thể xem bất kỳ tab nào mà chế độ vẫn tiếp tục chạy.
        """
        return

    def _toggle_input(self):
        if self.btn_input.isChecked():
            # Loại trừ: tắt 2 chế độ còn lại
            self._turn_off_read()
            self._turn_off_write_cont()
            self._apply_input_config()
            self.worker.set_mode(Mode.INPUT)
            self.tabs.setCurrentIndex(0)   # tự active tab Input
        else:
            self.worker.set_mode(Mode.IDLE)

    def _toggle_read(self):
        if self.btn_read.isChecked():
            self._turn_off_input()
            self._turn_off_write_cont()
            self.worker.set_mode(Mode.READ)
            self.tabs.setCurrentIndex(1)   # tự active tab Đọc
        else:
            self.worker.set_mode(Mode.IDLE)

    def _apply_input_config(self):
        if self.rb_uid.isChecked():
            src = "uid"
        elif self.rb_ndef.isChecked():
            src = "ndef"
        else:
            src = "auto"
        self.worker.set_input_config(
            src, self.chk_enter.isChecked(), "", list(self._postkeys))

    def _toggle_write_cont(self):
        """Bật/tắt chế độ GHI LIÊN TỤC. Nguồn: file txt (nếu đã chọn) hoặc ô nhập."""
        if self.btn_write_cont.isChecked():
            # Luôn chuyển sang tab Ghi trước (dù có nguồn ghi hay chưa)
            self.tabs.setCurrentIndex(2)
            use_file = bool(self._write_file_path and self._write_lines)
            if not use_file:
                url = self.in_write.text().strip()
                if not url:
                    self.btn_write_cont.setChecked(False)
                    self.in_write.setFocus()
                    QMessageBox.warning(self, tr("missing_content_title"), tr("missing_content_msg"))
                    return
            # loại trừ: tắt input và read
            self._turn_off_input()
            self._turn_off_read()
            if use_file:
                # Nguồn từ FILE: ghi lần lượt các dòng, reset con trỏ về đầu
                self.worker.set_write_lines(list(self._write_lines))
                self._rebuild_lines_view(current=0)
                self.lbl_file_progress.setText(
                    tr("file_progress", done=0, total=len(self._write_lines)))
                self._set_write_status(tr("write_status_file"), "info")
            else:
                # Nguồn từ Ô NHẬP (như cũ)
                self.worker.set_write_lines(None)
                self.worker.set_write_text(url)
                self._set_write_status(tr("write_status_cont"), "info")
            self.worker.set_mode(Mode.WRITE)
        else:
            self.worker.set_mode(Mode.IDLE)
            self._set_write_status(tr("write_status_idle"), "idle")

    def _set_write_status(self, text: str, kind: str = "idle"):
        """Dòng trạng thái CHẾ ĐỘ (nhẹ, không đậm). kind: idle | info."""
        fg = {"idle": "#888", "info": "#31708f"}
        self.lbl_write_status.setText(text)
        self.lbl_write_status.setStyleSheet(
            "padding:4px; color:%s;" % fg.get(kind, "#888"))

    def _set_write_result(self, text: str, ok: bool):
        """Dòng KẾT QUẢ GHI (màu nổi bật). Tự động xóa sau 1 giây."""
        bg = "#c6f6c6" if ok else "#f8d7da"
        fg = "#1b6b1b" if ok else "#a12b2b"
        self.lbl_write_result.setText(text)
        self.lbl_write_result.setStyleSheet(
            "padding:8px; border-radius:4px; font-weight:bold; background:%s; color:%s;"
            % (bg, fg))
        self.lbl_write_result.setVisible(True)
        # Tự động xóa sau 1s (restart timer mỗi lần có kết quả mới)
        self._write_result_timer.start(1000)

    def _do_write_clicked(self):
        url = self.in_write.text().strip()
        if not url:
            QMessageBox.warning(self, tr("missing_content_title"), tr("missing_content_msg"))
            return
        # đảm bảo các chế độ đang tắt trong lúc ghi 1 lần (loại trừ)
        self._turn_off_input()
        self._turn_off_read()
        self._turn_off_write_cont()
        self.worker.set_mode(Mode.IDLE)
        self.worker.request_write(url)
        self.append_log(tr("sent_write_req", url=url))

    # ---------------- Slots từ worker ----------------
    def append_log(self, msg: str):
        self.txt_log.append(msg)

    def on_connected(self, ok: bool, msg: str):
        self.lbl_conn.setText(("[OK] " if ok else "[LOI] ") + msg)
        self.lbl_conn.setStyleSheet(
            "font-weight:bold; color:%s;" % ("green" if ok else "red"))
        self.append_log(("[+] " if ok else "[-] ") + msg)
        if not ok:
            self.tray.showMessage(tr("tray_tooltip"), msg, QSystemTrayIcon.Critical, 4000)

    def on_card_read(self, uid: str, ndef: str):
        self.out_uid.setText(uid)
        self.out_ndef.setText(ndef if ndef else tr("no_ndef"))
        self.append_log(tr("log_read", uid=uid, ndef=ndef or "-"))

    def on_typed(self, uid: str, text: str):
        self.append_log(tr("log_input_typed", uid=uid, text=text))

    def on_write_result(self, ok: bool, msg: str):
        self.append_log(("[+] " if ok else "[-] ") + msg)
        icon = QSystemTrayIcon.Information if ok else QSystemTrayIcon.Warning
        self.tray.showMessage(tr("write_result_title"), msg, icon, 4000)
        # Hiển thị KẾT QUẢ sticky (không bị chế độ khác ghi đè)
        self._set_write_result(msg, ok)
        if ok:
            self._write_count += 1
            self.lbl_write_count.setText(tr("write_count", n=self._write_count))

    def on_mode_changed(self, mode: str):
        self.lbl_mode.setText(tr("mode_label", mode=mode.upper()))

    # ---------------- Tray / minimize ----------------
    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger or reason == QSystemTrayIcon.DoubleClick:
            self._show_from_tray()

    def _show_from_tray(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def changeEvent(self, event):
        # Minimize -> ẩn xuống tray
        if event.type() == event.WindowStateChange:
            if self.windowState() & Qt.WindowMinimized:
                QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(event)

    def _hide_to_tray(self):
        self.hide()
        self.tray.showMessage(
            tr("tray_tooltip"), tr("tray_running_bg"),
            QSystemTrayIcon.Information, 2500)

    def closeEvent(self, event):
        # Đóng cửa sổ -> ẩn xuống tray thay vì thoát (trừ khi Thoát từ tray)
        if self._quit_requested:
            self.worker.stop()
            self.worker.wait(2000)
            self.tray.hide()
            event.accept()
        else:
            event.ignore()
            self.hide()
            self.tray.showMessage(
                tr("tray_tooltip"), tr("tray_minimized"),
                QSystemTrayIcon.Information, 2500)

    def _real_quit(self):
        self._quit_requested = True
        self.close()

    def _refresh_device(self):
        """
        Làm mới / kiểm tra lại thiết bị NFC. Dùng khi thỉnh thoảng không nhận
        được device: dừng worker hiện tại (nhả cổng serial), dò lại cổng COM
        rồi kết nối lại.
        """
        # Tắt mọi chế độ đang chạy để tránh truy cập serial khi đang refresh
        self._turn_off_input()
        self._turn_off_read()
        self._turn_off_write_cont()

        self.btn_refresh.setEnabled(False)
        self.lbl_conn.setText(tr("refreshing"))
        self.lbl_conn.setStyleSheet("font-weight:bold; color:#31708f;")
        self.append_log(tr("log_refresh"))
        QApplication.processEvents()

        # Dừng worker cũ (nhả serial)
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)

        # Dò lại device: 0 -> báo lỗi; 1 -> dùng luôn; nhiều -> dialog chọn
        port = choose_device(self)
        if not port:
            # Không tìm thấy -> giữ nguyên trạng thái lỗi, cho phép thử lại
            self.lbl_conn.setText("[!] " + tr("dev_none_title"))
            self.lbl_conn.setStyleSheet("font-weight:bold; color:red;")
            self.btn_refresh.setEnabled(True)
            return

        # Kết nối lại với port mới chọn
        self.port = port
        self.setWindowTitle(f"{tr('app_title')}  [{port}]")
        self._start_worker(port)
        self.btn_refresh.setEnabled(True)


# ===================== Quyền Administrator =====================
def is_admin() -> bool:
    """Kiểm tra tiến trình có đang chạy với quyền Administrator không (Windows)."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _pythonw() -> str:
    """Trả về đường dẫn pythonw.exe (không console) nếu có, ngược lại python hiện tại."""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(candidate):
            return candidate
    return exe


def relaunch_as_admin() -> bool:
    """
    Khởi chạy lại chính script này với quyền Administrator qua UAC,
    dùng pythonw.exe để KHÔNG hiện cửa sổ cmd/console.
    Trả về True nếu đã yêu cầu elevate (tiến trình hiện tại nên thoát),
    False nếu không thể (người dùng từ chối UAC hoặc lỗi).
    """
    try:
        params = " ".join(f'"{a}"' for a in sys.argv)
        # ShellExecuteW với verb 'runas' -> bật UAC; dùng pythonw để ẩn console
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", _pythonw(), params, None, 1
        )
        # ret > 32 -> thành công
        return int(ret) > 32
    except Exception:
        return False


def relaunch_hidden_console() -> bool:
    """
    Nếu đang chạy bằng python.exe (có cửa sổ console), khởi chạy lại bằng
    pythonw.exe để ẩn console đi, rồi thoát tiến trình hiện tại.
    Chỉ làm khi tiến trình có console thực sự (tránh vòng lặp).
    Trả về True nếu đã relaunch (nên thoát).
    """
    try:
        exe = sys.executable.lower()
        if not exe.endswith("python.exe"):
            return False  # đã là pythonw hoặc đóng gói -> không cần
        pyw = _pythonw()
        if pyw == sys.executable:
            return False
        import subprocess
        subprocess.Popen([pyw] + sys.argv,
                         creationflags=0x08000000)  # CREATE_NO_WINDOW
        return True
    except Exception:
        return False


# ===================== Dialog chọn device =====================
class DevicePickerDialog(QDialog):
    """Hiện danh sách device NFC để người dùng chọn khi có nhiều hơn 1."""

    def __init__(self, devices: list):
        super().__init__()
        self.setWindowTitle(tr("dev_title"))
        self.setWindowIcon(make_icon())
        self.resize(420, 300)
        self.selected_port: Optional[str] = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("dev_prompt")))

        self.list = QListWidget()
        for dev, desc in devices:
            item = QListWidgetItem(f"{dev}  -  {desc}")
            item.setData(Qt.UserRole, dev)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _: self._accept())
        lay.addWidget(self.list, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _accept(self):
        item = self.list.currentItem()
        if item:
            self.selected_port = item.data(Qt.UserRole)
            self.accept()


# ===================== Dialog chọn ngôn ngữ =====================
class LanguagePickerDialog(QDialog):
    """Hiện lần đầu chạy để người dùng chọn ngôn ngữ."""

    def __init__(self):
        super().__init__()
        # Tiêu đề song ngữ để dễ hiểu trước khi chọn
        self.setWindowTitle("Chọn ngôn ngữ / Select language / 言語を選択")
        self.setWindowIcon(make_icon())
        self.resize(360, 240)
        self.selected_lang: Optional[str] = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Chọn ngôn ngữ / Select language / 言語を選択:"))

        self.list = QListWidget()
        for code, name in LANGUAGES:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, code)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        self.list.itemDoubleClicked.connect(lambda _: self._accept())
        lay.addWidget(self.list, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok)
        btns.accepted.connect(self._accept)
        lay.addWidget(btns)

    def _accept(self):
        item = self.list.currentItem()
        if item:
            self.selected_lang = item.data(Qt.UserRole)
            self.accept()


def ensure_language():
    """
    Đảm bảo đã có ngôn ngữ:
      - Nếu config đã lưu -> dùng luôn.
      - Nếu chưa (lần đầu chạy) -> hiện dialog cho chọn và lưu lại.
    """
    saved = load_saved_language()
    if saved:
        set_language(saved)
        return
    dlg = LanguagePickerDialog()
    dlg.exec_()
    lang = dlg.selected_lang or "en"
    save_language(lang)


def choose_device(parent=None) -> Optional[str]:
    """
    Logic chọn device theo yêu cầu:
      - 0 device  -> thông báo lỗi, trả về None
      - 1 device  -> dùng luôn làm mặc định
      - nhiều     -> hiện dialog cho chọn
    """
    devices = list_nfc_ports()  # list của (device, description)

    if not devices:
        QMessageBox.critical(parent, tr("dev_none_title"), tr("dev_none_msg"))
        return None

    if len(devices) == 1:
        return devices[0][0]

    dlg = DevicePickerDialog(devices)
    if dlg.exec_() == QDialog.Accepted and dlg.selected_port:
        return dlg.selected_port
    return None


def main():
    # 1) YÊU CẦU QUYỀN ADMINISTRATOR (chỉ áp dụng trên Windows)
    #    Trên Linux/macOS: bỏ qua: chạy thẳng (chế độ Input dùng pynput; trên
    #    Linux có thể cần chạy bằng sudo, trên macOS cần cấp quyền Accessibility).
    if IS_WINDOWS and not is_admin():
        # Thử khởi chạy lại với quyền admin qua UAC
        if relaunch_as_admin():
            sys.exit(0)  # tiến trình mới (elevated) sẽ chạy; thoát tiến trình này
        else:
            # Không elevate được (người dùng từ chối UAC) -> báo lỗi và thoát
            app = QApplication(sys.argv)
            # Chọn ngôn ngữ đã lưu (nếu có) để hiện đúng lỗi
            saved = load_saved_language()
            if saved:
                set_language(saved)
            QMessageBox.critical(None, tr("admin_title"), tr("admin_msg"))
            sys.exit(1)

    # 1b) ẨN CỬA SỔ CONSOLE (cmd) - chỉ Windows: nếu đang chạy bằng python.exe
    #     có console, relaunch bằng pythonw.exe (CREATE_NO_WINDOW) rồi thoát.
    if IS_WINDOWS and relaunch_hidden_console():
        sys.exit(0)

    # 2) KHỞI TẠO GUI
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # cho phép chạy ngầm khi đóng cửa sổ

    # 2b) NGÔN NGỮ: lần đầu chạy -> cho chọn; sau đó dùng config
    ensure_language()

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.warning(None, tr("tray_na_title"), tr("tray_na_msg"))

    # 3) CHỌN DEVICE (0 -> lỗi/thoát, 1 -> mặc định, nhiều -> cho chọn)
    port = choose_device()
    if not port:
        sys.exit(1)

    win = MainWindow(port)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

