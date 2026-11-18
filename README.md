# Gree Local Monitor

Raspberry Pi-n futó, helyi hálózatról elérhető webes alkalmazás Gree Wi-Fi klímaadatok lekéréséhez és USB-soros adatok valós idejű megjelenítéséhez.

## Funkciók

- Gree Wi-Fi klímák automatikus vagy célzott helyi felderítése;
- bekapcsolt állapot, üzemmód, szoba- és célhőmérséklet, ventilátor és kiegészítő állapotok lekérése;
- egyszerű HTTP/JSON API a klímaadatokhoz;
- több, névvel és fix IP-címmel menthető klímaprofil;
- kétirányú, szemantikus HTTP/JSON gateway és importálható TapHome Packet Parser sablon;
- automatikus `/dev/ttyUSB*` és `/dev/ttyACM*` portfelismerés;
- élő adatátvitel a böngészőbe Server-Sent Events kapcsolaton;
- szöveges és HEX nyersadat-nézet;
- JSON mezők és egyszerű numerikus értékek automatikus grafikonja;
- csomag-, bájt- és adatsebesség-statisztika;
- szüneteltetés, helyi törlés és JSON-export;
- beépített próbaadat hardver nélküli teszteléshez;
- reszponzív felület telefonra, tabletre és monitorra.

## Gyors indítás

Python 3.10 vagy újabb szükséges.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py --demo
```

Ezután nyisd meg a Pi-n a `http://localhost:8080` címet. Másik gépről a helyi hálózaton a Pi címével érhető el, például `http://192.168.1.42:8080`.

## Gree klíma előkészítése

A klímát először a Gree+ mobilalkalmazással csatlakoztasd ahhoz a helyi Wi-Fi hálózathoz, amelyen a Raspberry Pi is van. Az alkalmazás ezután közvetlenül, a Gree helyi UDP/7000 protokollján kommunikál a klímával; a Gree felhőt nem használja.

A webes felületen több klíma helyi IPv4-címét is elmentheted egyedi profilnévvel, például `Nappali` vagy `Halo_emelet`. A profilnévben betű, szám, aláhúzás és kötőjel használható. Fix DHCP-foglalás ajánlott, hogy a klímák IP-címe ne változzon.

## Klíma HTTP API

Felderítés a teljes helyi hálózaton:

```bash
curl -X POST http://raspberry-pi:8090/api/ac/scan \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Célzott felderítés ismert IP-címmel:

```bash
curl -X POST http://raspberry-pi:8090/api/ac/scan \
  -H 'Content-Type: application/json' \
  -d '{"target":"192.168.1.73"}'
```

Felderített eszközök listája és egy klíma aktuális állapota:

```bash
curl http://raspberry-pi:8090/api/ac/devices
curl 'http://raspberry-pi:8090/api/ac/status?device=AABBCC112233'
```

Ha pontosan egy klíma van felderítve, a `device` paraméter elhagyható.

Névvel ellátott profilok kezelése:

```bash
curl http://raspberry-pi:8090/api/ac/profiles
curl -X POST http://raspberry-pi:8090/api/ac/profiles \
  -H 'Content-Type: application/json' \
  -d '{"name":"Nappali","ip":"192.168.1.73"}'
curl -X POST http://raspberry-pi:8090/api/ac/profiles/delete \
  -H 'Content-Type: application/json' \
  -d '{"name":"Nappali"}'
```

## TapHome Packet Parser gateway

A gateway nem ad ki Modbus-regisztercímeket vagy kódolt regiszterértékeket. A klíma állapota lapos, névvel ellátott JSON-mezőként olvasható, a vezérelhető mezők pedig ugyanezen a néven írhatók. A mellékelt TapHome Packet Parser sablon ezeket natív kapcsolóként, termosztátként és többértékű választókként jeleníti meg.

TapHome beállítás:

1. Töltsd le a sablont: `http://RASPBERRY-PI-IP:8090/api/taphome/template.xml`.
2. A TapHome-ban nyisd meg: **Hardware → Packet Parser → Add from file**.
3. Importáld a letöltött `Gree_AC_PacketParser.xml` fájlt.
4. Add meg a Raspberry Pi fix helyi IP-címét, a telepítéskor választott HTTP-portot és a webes felületen elmentett klímaprofil nevét.

Az állapot lekérése:

```bash
curl http://raspberry-pi:8090/api/taphome/ac/Nappali
```

Példaválasz:

```json
{
  "climateName": "Nappali",
  "power": true,
  "mode": "cool",
  "currentTemperature": 23.5,
  "targetTemperature": 22,
  "fanSpeed": "medium",
  "verticalSwing": "fullswing",
  "horizontalSwing": "center",
  "sleep": false,
  "quiet": false
}
```

Egy vagy több beállítás módosítása ugyanazon a végponton történik:

```bash
curl -X POST http://raspberry-pi:8090/api/taphome/ac/Nappali \
  -H 'Content-Type: application/json' \
  -d '{"power":true,"mode":"cool","targetTemperature":22}'
```

Olvasható: be-/kikapcsolás, üzemmód, aktuális és célhőmérséklet, ventilátor, függőleges és vízszintes légterelő, páratartalom, frisslevegő-, csendes-, turbó-, alvás-, fény-, X-Fan-, ionizátor- és szűrőállapot. A szűrő és a mért értékek csak olvashatók; a többi felsorolt vezérlés az adott klímamodell támogatásától függően írható.

Ha egynél több profil van mentve, a profilnév kötelező az API URL-ben. Egyetlen profil esetén a név nélküli `/api/taphome/ac` végpont is automatikusan azt használja.

## Telepítő Raspberry Pi-re

A telepítő bekéri a webes felület portját, létrehozza a virtuális Python környezetet, telepíti a függőségeket, majd beállítja és elindítja a systemd szolgáltatást. A mentett klímaprofilok a `/var/lib/gree-usb-monitor/climates.json` fájlban maradnak meg. Az ajánlott alapértelmezett port `8090`, így nem ütközik a már használt `8080`-as porttal.

Interaktív telepítés:

```bash
chmod +x install.sh
sudo ./install.sh
```

Automatizált telepítés megadott porttal:

```bash
sudo ./install.sh --port 8090
```

Másik Linux-felhasználó megadása:

```bash
sudo ./install.sh --port 8090 --user pi
```

A portnak `1024` és `65535` közé kell esnie. A telepítő jelzi, ha a kiválasztott port már foglalt. Új port beállításához később egyszerűen futtasd újra a telepítőt másik `--port` értékkel.

## Valódi USB-eszköz

Csatlakoztasd az USB-soros eszközt, majd indítsd el a szervert:

```bash
python server.py
```

A port és a baud rate a webes felületen választható ki. Automatikus kapcsolódás is használható:

```bash
python server.py --serial-port /dev/ttyUSB0 --baudrate 115200
```

Ha a felhasználó nem fér hozzá a soros porthoz, add hozzá a `dialout` csoporthoz, majd jelentkezz ki és vissza:

```bash
sudo usermod -aG dialout "$USER"
```

## Beállítások

A parancssori kapcsolók helyett környezeti változók is használhatók. A telepítő ezeket az `/opt/gree-usb-monitor/.env` fájlba írja.

| Változó | Alapérték | Leírás |
| --- | --- | --- |
| `GREE_HOST` | `0.0.0.0` | Figyelési cím |
| `GREE_PORT` | `8080` | HTTP-port |
| `GREE_SERIAL_PORT` | — | Automatikusan megnyitandó eszköz |
| `GREE_BAUDRATE` | `115200` | Soros sebesség |
| `GREE_AC_HOST` | — | Klíma fix helyi IPv4-címe a célzott felderítéshez |
| `GREE_CONFIG_PATH` | `data/climates.json` | A névvel ellátott klímaprofilok JSON-fájlja; telepítve `/var/lib/gree-usb-monitor/climates.json` |
| `GREE_TAPHOME_DEVICE` | — | Klíma MAC- vagy IPv4-címe, ha több eszköz van |
| `GREE_TAPHOME_CACHE_SECONDS` | `1` | A Packet Parser lekérések között megosztott klímaállapot gyorsítótára |
| `GREE_DEMO` | `0` | `1` esetén próbaadat indul |
| `GREE_QUIET` | `0` | `1` esetén nincs HTTP access log |

Megjegyzés: a Python nem olvassa be automatikusan a `.env` fájlt. A systemd egység igen; kézi indításnál exportáld a kívánt változókat.

## Kézi systemd telepítés

Normál esetben használd a fenti telepítőt. Kézi beállításhoz másold a repót az `/opt/gree-usb-monitor` könyvtárba, hozd létre a virtuális környezetet, majd a `deploy/gree-usb-monitor.service` sablonban cseréld le a `__SERVICE_USER__`, `__SERVICE_GROUP__` és `__INSTALL_DIR__` helyőrzőket. A sablon közvetlenül, helyőrzők cseréje nélkül nem telepíthető.

## Teszt

```bash
python -m unittest discover -s tests -v
```

Az alkalmazás nem küld adatot felhőbe; a klímakommunikáció, a soros adat és a böngészős felület a helyi hálózaton marad.
