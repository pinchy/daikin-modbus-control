#!/usr/bin/env python3
"""Probe a Modbus TCP device to discover available registers."""

import socket
import struct
import sys

def modbus_read(sock, func_code, address, count, unit_id=1, tid=1):
    """Send a Modbus read request and return raw register values."""
    # MBAP Header + PDU
    request = struct.pack('>HHHBBHH',
        tid,        # Transaction ID
        0,          # Protocol ID
        6,          # Length
        unit_id,    # Unit ID
        func_code,  # Function code (0x03=holding, 0x04=input)
        address,    # Start address
        count       # Quantity
    )
    sock.sendall(request)

    # Read response header
    header = sock.recv(256)
    if not header or len(header) < 9:
        return None

    # Check for exception
    if header[7] & 0x80:
        return None

    byte_count = header[8]
    data = header[9:9+byte_count]

    registers = []
    for i in range(0, len(data), 2):
        if i+1 < len(data):
            registers.append(struct.unpack('>H', data[i:i+2])[0])
    return registers


def try_device_id(sock, unit_id=1):
    """Try Modbus Device Identification (function 0x2B, MEI 0x0E)."""
    tid = 9999
    # MEI request: Read Device ID, category 01 (basic), object 0
    request = struct.pack('>HHHBBBBB',
        tid,        # Transaction ID
        0,          # Protocol ID
        5,          # Length
        unit_id,    # Unit ID
        0x2B,       # Function code (MEI)
        0x0E,       # MEI type (Read Device ID)
        0x01,       # Read Device ID code (basic)
        0x00        # Object ID (start from 0)
    )
    sock.sendall(request)

    try:
        resp = sock.recv(512)
        if resp and len(resp) > 9 and not (resp[7] & 0x80):
            print("\n=== Device Identification (0x2B) ===")
            print(f"  Raw: {resp.hex()}")
            # Parse objects
            try:
                idx = 13  # skip MBAP(7) + FC(1) + MEI(1) + ReadDevId(1) + Conformity(1) + More(1) + ObjCount(1)
                obj_count = resp[12] if len(resp) > 12 else 0
                for _ in range(obj_count):
                    if idx + 2 > len(resp):
                        break
                    obj_id = resp[idx]
                    obj_len = resp[idx+1]
                    obj_val = resp[idx+2:idx+2+obj_len]
                    names = {0: "VendorName", 1: "ProductCode", 2: "MajorMinorRevision",
                             3: "VendorUrl", 4: "ProductName", 5: "ModelName", 6: "UserAppName"}
                    name = names.get(obj_id, f"Object_{obj_id}")
                    print(f"  {name}: {obj_val.decode('ascii', errors='replace')}")
                    idx += 2 + obj_len
            except Exception as e:
                print(f"  (parse error: {e})")
            return True
        else:
            print("\n=== Device Identification (0x2B) === NOT SUPPORTED")
            return False
    except Exception:
        print("\n=== Device Identification (0x2B) === TIMEOUT/ERROR")
        return False


def format_as_mac(regs):
    """Try to interpret 3 registers as a MAC address."""
    if len(regs) < 3:
        return None
    b = []
    for r in regs[:3]:
        b.append((r >> 8) & 0xFF)
        b.append(r & 0xFF)
    mac = ':'.join(f'{x:02X}' for x in b)
    # Check if it looks like a real MAC (not all zeros or all FF)
    if all(x == 0 for x in b) or all(x == 0xFF for x in b):
        return None
    return mac


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 modbus_probe.py <IP> [port]")
        print("Example: python3 modbus_probe.py 192.168.50.100")
        sys.exit(1)

    host = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 502

    print(f"Connecting to {host}:{port}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect((host, port))
    print("Connected!\n")

    tid = 1

    # 1) Read known registers first
    print("=== Known Registers ===")
    known = [
        (0x03, 2000, 1, "Power/Mode"),
        (0x03, 2001, 1, "Setpoint"),
        (0x03, 2003, 1, "Fan Speed"),
        (0x04, 2005, 1, "Room Temp"),
        (0x04, 2006, 1, "Outdoor Temp"),
    ]
    for fc, addr, cnt, name in known:
        regs = modbus_read(sock, fc, addr, cnt, tid=tid)
        tid += 1
        if regs:
            print(f"  {name:20s} @ {addr}: {regs}  (hex: {' '.join(f'0x{r:04X}' for r in regs)})")
        else:
            print(f"  {name:20s} @ {addr}: <no response>")

    # 2) Try Device Identification
    try_device_id(sock)

    # 3) Scan holding registers (0x03) in interesting ranges
    print("\n=== Scanning Holding Registers (FC 0x03) ===")
    scan_ranges = [
        (0, 50, "Low range 0-49"),
        (100, 120, "Range 100-119"),
        (200, 220, "Range 200-219"),
        (1000, 1020, "Range 1000-1019"),
        (2000, 2020, "Range 2000-2019"),
        (3000, 3020, "Range 3000-3019"),
        (4000, 4020, "Range 4000-4019"),
        (9000, 9020, "Range 9000-9019"),
    ]

    for start, end, label in scan_ranges:
        print(f"\n  --- {label} ---")
        found_any = False
        for addr in range(start, end):
            try:
                regs = modbus_read(sock, 0x03, addr, 1, tid=tid)
                tid += 1
                if regs is not None:
                    val = regs[0]
                    # Show non-zero values, or zero if it's a valid response
                    hex_str = f"0x{val:04X}"
                    ascii_hi = chr((val >> 8) & 0x7F) if 0x20 <= ((val >> 8) & 0x7F) < 0x7F else '.'
                    ascii_lo = chr(val & 0x7F) if 0x20 <= (val & 0x7F) < 0x7F else '.'
                    extra = f"  ascii: '{ascii_hi}{ascii_lo}'" if val != 0 else ""
                    print(f"    [{addr:5d}] = {val:6d}  ({hex_str}){extra}")
                    found_any = True
            except Exception:
                pass
        if not found_any:
            print(f"    (no valid registers)")

    # 4) Scan input registers (0x04) in interesting ranges
    print("\n=== Scanning Input Registers (FC 0x04) ===")
    input_ranges = [
        (0, 50, "Low range 0-49"),
        (100, 120, "Range 100-119"),
        (2000, 2020, "Range 2000-2019"),
        (3000, 3020, "Range 3000-3019"),
    ]

    for start, end, label in input_ranges:
        print(f"\n  --- {label} ---")
        found_any = False
        for addr in range(start, end):
            try:
                regs = modbus_read(sock, 0x04, addr, 1, tid=tid)
                tid += 1
                if regs is not None:
                    val = regs[0]
                    hex_str = f"0x{val:04X}"
                    ascii_hi = chr((val >> 8) & 0x7F) if 0x20 <= ((val >> 8) & 0x7F) < 0x7F else '.'
                    ascii_lo = chr(val & 0x7F) if 0x20 <= (val & 0x7F) < 0x7F else '.'
                    extra = f"  ascii: '{ascii_hi}{ascii_lo}'" if val != 0 else ""
                    print(f"    [{addr:5d}] = {val:6d}  ({hex_str}){extra}")
                    found_any = True
            except Exception:
                pass
        if not found_any:
            print(f"    (no valid registers)")

    # 5) Try reading 3 consecutive registers at common MAC locations
    print("\n=== Possible MAC Address Locations ===")
    mac_candidates = [0, 3, 6, 10, 100, 200, 1000, 3000]
    for addr in mac_candidates:
        try:
            regs = modbus_read(sock, 0x03, addr, 3, tid=tid)
            tid += 1
            if regs and len(regs) == 3:
                mac = format_as_mac(regs)
                if mac:
                    print(f"  Holding [{addr}..{addr+2}]: {mac}")
        except Exception:
            pass
        try:
            regs = modbus_read(sock, 0x04, addr, 3, tid=tid)
            tid += 1
            if regs and len(regs) == 3:
                mac = format_as_mac(regs)
                if mac:
                    print(f"  Input   [{addr}..{addr+2}]: {mac}")
        except Exception:
            pass

    sock.close()
    print("\nDone.")


if __name__ == '__main__':
    main()
