import math
import logging
import time
from dataclasses import dataclass
from typing import Optional

from pymavlink import mavutil
import serial as _serial

log = logging.getLogger(__name__)


@dataclass
class GPSFix:
    lat: float
    lon: float
    alt_rel: float
    heading: float
    groundspeed: float
    timestamp: float


def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class PixhawkBridge:
    def __init__(self, connection_string, baud=57600):
        self._conn_str = connection_string
        self._baud = baud
        self._mav = None

    def connect(self):
        log.info("Connecting to Pixhawk on %s @ %d baud ...", self._conn_str, self._baud)
        self._mav = mavutil.mavlink_connection(self._conn_str, baud=self._baud)
        self._mav.wait_heartbeat(timeout=30)
        log.info("Connected — heartbeat received (sysid=%d)", self._mav.target_system)
        self._mav.mav.request_data_stream_send(
            self._mav.target_system,
            self._mav.target_component,
            mavutil.mavlink.MAV_DATA_STREAM_ALL,
            4, 1
        )

    def disconnect(self):
        if self._mav:
            self._mav.close()
            self._mav = None

    def get_gps(self) -> Optional[GPSFix]:
        if self._mav is None:
            raise RuntimeError("Not connected")
        msg = self._mav.recv_match(type=["GLOBAL_POSITION_INT"], blocking=True, timeout=2.0)
        if msg is None:
            return None
        lat = msg.lat / 1e7
        lon = msg.lon / 1e7
        if lat == 0.0 and lon == 0.0:
            return None
        alt_rel = msg.relative_alt / 1000.0
        heading = msg.hdg / 100.0 if msg.hdg != 65535 else 0.0
        groundspeed = math.sqrt(msg.vx ** 2 + msg.vy ** 2) / 100.0
        return GPSFix(
            lat=lat, lon=lon, alt_rel=alt_rel,
            heading=heading, groundspeed=groundspeed,
            timestamp=time.time(),
        )

    def gps_fix_type(self) -> int:
        msg = self._mav.recv_match(type=["GPS_RAW_INT"], blocking=True, timeout=2.0)
        return msg.fix_type if msg else 0

    def wait_for_gps(self, min_fix_type=3, timeout=60.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.gps_fix_type() >= min_fix_type:
                return
            time.sleep(1.0)
        raise TimeoutError("No GPS fix")

    def has_arrived(self, target_lat, target_lon, radius_m) -> bool:
        fix = self.get_gps()
        if fix is None:
            return False
        return haversine_distance(fix.lat, fix.lon, target_lat, target_lon) <= radius_m


class HATGPSBridge:
    """GPS via Waveshare SIM7600X HAT using AT+CGPSINFO on /dev/ttyUSB2."""

    def __init__(self, port="/dev/ttyUSB2", baud=115200):
        self._port = port
        self._baud = baud
        self._serial = None

    def connect(self):
        try:
            self._serial = _serial.Serial(self._port, baudrate=self._baud, timeout=2)
        except _serial.SerialException as e:
            raise RuntimeError(f"GPS port {self._port} nicht verfügbar: {e}")
        self._serial.write(b'AT+CGPS=0\r\n')
        time.sleep(1)
        self._serial.read(100)
        self._serial.write(b'AT+CGPS=1,1\r\n')
        time.sleep(2)
        resp = self._serial.read(100).decode(errors='ignore')
        if 'OK' in resp:
            log.info("HAT GPS started on %s", self._port)
        else:
            log.warning("HAT GPS start: %s", repr(resp))
        log.info("HAT GPS connected on %s @ %d", self._port, self._baud)

    def disconnect(self):
        if self._serial:
            self._serial.close()
            self._serial = None

    def _parse_cgpsinfo(self, resp):
        for line in resp.split('\n'):
            if '+CGPSINFO:' in line:
                parts = line.strip().replace('+CGPSINFO:', '').strip().split(',')
                if len(parts) >= 4 and parts[0] and parts[2]:
                    try:
                        lat_raw = float(parts[0])
                        lat_dir = parts[1].strip()
                        lon_raw = float(parts[2])
                        lon_dir = parts[3].strip()
                        lat = int(lat_raw / 100) + (lat_raw % 100) / 60.0
                        if lat_dir == 'S':
                            lat = -lat
                        lon = int(lon_raw / 100) + (lon_raw % 100) / 60.0
                        if lon_dir == 'W':
                            lon = -lon
                        alt = float(parts[6]) if len(parts) > 6 and parts[6] else 0.0
                        return lat, lon, alt
                    except (ValueError, IndexError):
                        return None
        return None

    def get_gps(self) -> Optional[GPSFix]:
        if self._serial is None or not self._serial.is_open:
            try:
                self.connect()
            except Exception as e:
                log.warning("GPS reconnect fehlgeschlagen: %s", e)
                return None
        try:
            self._serial.reset_input_buffer()
            self._serial.write(b'AT+CGPSINFO\r\n')
            time.sleep(1)
            resp = self._serial.read(200).decode(errors='ignore')
        except _serial.SerialException as e:
            log.warning("HAT GPS SerialException: %s — reconnect beim nächsten Aufruf", e)
            self._serial = None
            return None
        except Exception as e:
            log.warning("HAT GPS read error: %s", e)
            time.sleep(2)
            return None
        result = self._parse_cgpsinfo(resp)
        if result is None:
            return None
        lat, lon, alt = result
        return GPSFix(lat=lat, lon=lon, alt_rel=alt, heading=0.0,
                      groundspeed=0.0, timestamp=time.time())

    def wait_for_gps(self, timeout=600.0):
        log.info("Waiting for HAT GPS fix (max %.0f sec)...", timeout)
        deadline = time.time() + timeout
        while time.time() < deadline:
            fix = self.get_gps()
            if fix is not None:
                log.info("HAT GPS fix acquired: lat=%.6f lon=%.6f", fix.lat, fix.lon)
                return
            time.sleep(3.0)
        raise TimeoutError("No GPS fix from HAT")
