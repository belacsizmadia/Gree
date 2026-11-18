# Gree USB Monitor

Raspberry Pi-n futó, helyi hálózatról elérhető webes alkalmazás USB-soros adatok valós idejű megjelenítéséhez.

## Funkciók

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

## Telepítő Raspberry Pi-re

A telepítő bekéri a webes felület portját, létrehozza a virtuális Python környezetet, telepíti a függőségeket, majd beállítja és elindítja a systemd szolgáltatást. Az ajánlott alapértelmezett port `8090`, így nem ütközik a már használt `8080`-as porttal.

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

A parancssori kapcsolók helyett környezeti változók is használhatók. A minta az [`.env.example`](.env.example) fájlban található.

| Változó | Alapérték | Leírás |
| --- | --- | --- |
| `GREE_HOST` | `0.0.0.0` | Figyelési cím |
| `GREE_PORT` | `8080` | HTTP-port |
| `GREE_SERIAL_PORT` | — | Automatikusan megnyitandó eszköz |
| `GREE_BAUDRATE` | `115200` | Soros sebesség |
| `GREE_DEMO` | `0` | `1` esetén próbaadat indul |
| `GREE_QUIET` | `0` | `1` esetén nincs HTTP access log |

Megjegyzés: a Python nem olvassa be automatikusan a `.env` fájlt. A systemd egység igen; kézi indításnál exportáld a kívánt változókat.

## Kézi systemd telepítés

Normál esetben használd a fenti telepítőt. Kézi beállításhoz másold a repót az `/opt/gree-usb-monitor` könyvtárba, hozd létre a virtuális környezetet, majd a `deploy/gree-usb-monitor.service` sablonban cseréld le a `__SERVICE_USER__`, `__SERVICE_GROUP__` és `__INSTALL_DIR__` helyőrzőket. A sablon közvetlenül, helyőrzők cseréje nélkül nem telepíthető.

## Teszt

```bash
python -m unittest discover -s tests -v
```

Az alkalmazás nem küld adatot felhőbe; a soros adat a Raspberry Pi és az azonos helyi hálózaton megnyitott böngésző között marad.
