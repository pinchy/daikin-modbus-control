# Daikin Local Control

Local-only control for Daikin Zena (FTXJ) air conditioners via Modbus TCP. No cloud, no external connections, no data leaving your network.

## Setup

1. Edit `config.json` with your unit names and IP addresses
2. Run:

```bash
chmod +x start.sh
./start.sh
```

3. Open `http://<your-mac-ip>:8080` on your iPhone
4. Tap Share → Add to Home Screen for native app feel

## The Journey: a dive into modbus

I have a number of Daikin Zena aircon (or heatpumps for my America friends) installed at our house. I spent a bit more to buy the "wifi connection" module thinking I could wire it all up to my homeassisant server and have blissful home automation.

However the one thing that I've learned about home automation is that it's never blissful. And the native Daikin app for control is ... pretty flakey. It calls home, it takes a good 10-15seconds just to bring up a device and it's super laggy. Which is not what I want at 2am when in a dazed slumber I want to turn on the unit to bring some fresh cool air to counteract Sydney's scorching summer.

On my router I've blocked outbound connections from each (IOT appliance 101) and I can still control it from the app, so it's not portfowrading or opening up a websocket to the cloud for control - the app must be doing local control. So why is the app so sluggish?

There are plenty of open-source projects that use Daikin's HTTP API, so I figured it'd be straightforward. But alas, it was not. It looks like basic HTTP API has been deprecated.

### Hitting a wall with the HTTP API

From my router, I knew each unit's IP and I gave the HTTP endpoint a go:

```
curl 192.168.1.80/aircon/get_sensor_info
curl: (7) Failed to connect to 192.168.1.80 port 80 after 5 ms: Couldn't connect to server
```

Port 80 wasn't even open. OK, what _is_ open?

```
nmap -sS -p 1-65535 192.168.1.80
```

![Moments later](readme/moments-later.png)

```
PORT    STATE SERVICE
443/tcp open  https
502/tcp open  mbap
```

Two ports. HTTPS and something called `mbap`. I tried HTTPS first — the old API path, just with TLS:

```
curl -k -v https://192.168.1.80/common/basic_info
```

The connection worked, but:

```
HTTP/1.0 403 HTTP_FORBIDDEN
Content-Length: 0
```

The verbose output told the story — the server certificate was issued by `DAIKIN INDUSTRIES, LTD` with a CN starting with `015F4441494B494E`. Searching online reveals this is the newer BRP069C adapter (woot! progress), and it uses **mutual TLS (mTLS)**. But without a client certificate (which is baked into the Daikin app), the HTTPS API is a dead end. I thought aobut doing a MITM attack to intercept the certificate but first, that other port.

502 — that's **Modbus TCP**. An industrial control protocol. On a home air conditioner. I use it at $DAY_JOB. I know this.

![I know this](readme/unix-system.jpg)

If you're not familiar with Modbus protocol, basically there's a bunch of registers, which can be read only (for say sensor values, like temperature) or they could be read/write (for settings, or turning on things). (I'm glossing, there are other types, e.g. "coils" that were to energise oldschool relay coils and are binary (this is indusrial control remember?), but basically for our purpose, there are read only things and writeable things).

### Initiating First contact

Ok, let's see if we can slither our way to a connection:

```python
from pymodbus.client import ModbusTcpClient
client = ModbusTcpClient('192.168.1.80', port=502)
client.connect()
result = client.read_holding_registers(address=0, count=10)
print(result.registers if not result.isError() else result)
```

```
ExceptionResponse(dev_id=1, function_code=131, exception_code=2)
```

Woot! We're getting some chatter! Exception code 2 means "Illegal Data Address" so it's communicating, just telling me I'm looking in the wrong spot. In other words, there are registers, just not at address 0. Let's try them all!

### Scanning for registers

I hit a bunch of common address ranges:

```python
for start in [0, 1, 10, 20, 30, 100, 200, 300, 400, 500, 1000, 2000, 3000, 4000]:
    result = client.read_holding_registers(address=start, count=1)
    if not result.isError():
        print(f'Found data at register {start}: {result.registers}')
```

```
Found data at register 2000: [512]
Found input register at 0: [257]
Found input register at 1: [256]
Found input register at 1000: [31]
Found input register at 2000: [512]
```

Data in the 2000 range. Ok let's try incrementing by 1. And let's see if I can tell the difference between a holding (read only) register or an input register (writable)

```
python3 -c "
from pymodbus.client import ModbusTcpClient
client = ModbusTcpClient('192.168.1.80', port=502)
client.connect()

print('=== HOLDING REGISTERS ===')
for addr in range(1990, 2050):
    result = client.read_holding_registers(address=addr, count=1)
    if not result.isError():
        print(f'  HR {addr}: {result.registers[0]}')

print('=== INPUT REGISTERS ===')
for addr in range(0, 50):
    result = client.read_input_registers(address=addr, count=1)
    if not result.isError():
        print(f'  IR {addr}: {result.registers[0]}')

for addr in range(990, 1050):
    result = client.read_input_registers(address=addr, count=1)
    if not result.isError():
        print(f'  IR {addr}: {result.registers[0]}')

for addr in range(1990, 2050):
    result = client.read_input_registers(address=addr, count=1)
    if not result.isError():
        print(f'  IR {addr}: {result.registers[0]}')

print('Scan complete')
client.close()
"
```

This should give us the full register map. The values will likely include room temp, setpoint, mode, fan speed, etc.

```


=== HOLDING REGISTERS ===
  HR 2000: 512
  HR 2001: 220
  HR 2002: 0
  HR 2003: 11
  HR 2004: 0
  HR 2008: 0
  HR 2010: 0
=== INPUT REGISTERS ===
  IR 0: 257
  IR 1: 256
  IR 2: 1
  IR 3: 16
  IR 4: 0
  IR 1000: 31
  IR 1001: 20565
  IR 1002: 5
  IR 1003: 87
  IR 1004: 3
  IR 1005: 11
  IR 1006: 0
  IR 2000: 512
  IR 2001: 220
  IR 2002: 0
  IR 2003: 11
  IR 2004: 0
  IR 2005: 280
  IR 2006: 260
  IR 2007: 0
  IR 2008: 0
  IR 2009: 0
  IR 2010: 0
Scan complete
```

Yes!

### Decoding the values

Ok, I can probably guess a few of these, 280, 260, 220 - these are probably temps x10 (the Daikin app works to 1dp), so 28, 26, 22 degrees. No idea on 11, or 20565. Let's see what the Daikin unit is actually doing. Copying the data from the official app I see:

- Setpoint: 22 C
- Power: off
- Mode: cooling
- Room temp: 28 C
- Outdoor temp: 26 C
- Fan: quiet mode

Ok mapping these give sus:

- **HR 2001: 220** — that's the setpoint: 22.0 C (value / 10)
- **IR 2005: 280** — room temperature: 28.0 C
- **IR 2006: 260** — outdoor temperature: 26.0 C
- **HR 2000: 512 (0x0200)** — NFI
- **HR 2003: 11** — NFI

To work out what 512 and 11 are, I'm going to have to change things.

### Mapping the Registers

Without changing any other settings, I turned the unit on via the app and ran the scan again, one register changed:

```
HR 2000: 512 -> 513
```

Bit 0 flipped. `0x0200` (off) became `0x0201` (on). So bit 0 is power, upper byte is something else. I'd associate the "device mode" with "power" so lets see what happens to the something else when I change the mode from cooling to say fan.

```
dry -> 0x0101
cool -> 0x0201
heat -> 0x0301
fan -> 0x0401
```

Ok, so looks like the something is the mode. Win!

And although I couldn't see it, as the register immediately changed to the desired mode, switching it to 0x0001 was "auto".

After repeating the same process with fan speed, register 2003 was determined to be the fan speed, auto, quiet, 1,2,3,4,5.

```
auto -> 0
quiet -> 11
1 -> 3
2 -> 4
3 -> 5
4 -> 6
5 -> 7
```

### Let's change things up

Ok, this is fun, but doesn't help my 2am drowsey self wanting to turn something on. Let's see if we can control it!

```python
result = client.write_register(address=2000, value=512)
```

**The unit turned off.** Full control, no authentication, no certificates, no cloud. Just register writes over the local network using industrial modbus that I use at work. Love it.

### Ok, let's build an app

I'm not going to lie to you, I vibe coded the app. Running on my home server, it reads the config.json file for ip addresses and expsoses itself as a web browser that I can open as a PWA on my phone, icon saved to the home screen. This is this code base. To run it, download the repo, run start.sh and it will take care of the rest. Note the computer has to be on the same subnet as the aircon devices.

However it's still a little sluggish the first time you connect as the server has to spool up. I stupidly asked if js could control modbus directly (it can with bluetooth, so why not?) but it cant. Hours later I realised, I could vibe code a swift app which could!

Armed with the register map and how the protocol worked, I used AgentKan - a kanban dashboard for orchestrating agents - to code up a native swift app. Much faster. Once my apple dev account gets approved I'll see if I can stick it in the app store.

The result? almost instant control of my a/c units. No calling home. No sluggy behaviour. Just instant control.

## Key Takeaways

1. **Port scan everything.** The documented HTTP API was locked behind mTLS, but Modbus was wide open on port 502 >:D
2. **Modbus is simple.** It's just register reads and writes. The only tricky part is working out what the reigsters do. Trial and error is simple. This isn't a nuclear reactor I can shutdown, it's just an A/C unit.
3. **Cross-reference with the app.** Knowing the current state from the official app made it easy to decode what each register meant.

## Register Map

In summary, here's a map of the registers I care about:

| Register | Type | Description                                                 |
| -------- | ---- | ----------------------------------------------------------- |
| HR 2000  | R/W  | Power (bit 0) + Mode (upper byte: 0x02 = cool, 0x03 = heat) |
| HR 2001  | R/W  | Setpoint (/10 = °C)                                         |
| HR 2003  | R/W  | Fan speed                                                   |
| IR 2005  | R/O  | Room temperature (/10 = °C)                                 |
| IR 2006  | R/O  | Outdoor temperature (/10 = °C)                              |

## Disclaimer

I don't work for Daikin, I have nothing to do with Daikin, all Daikin trademarks belong to the Daikin company etc etc.
