#!/usr/bin/env python3
"""
PeekaBaby — native app-schil (pywebview) rond de branded HTML-UI.

Vervangt de kale Tkinter-UI: toont ui/index.html in een WebView2-venster en
stelt een Python-API beschikbaar (login, camera's, streamen). De video speelt
IN de app via go2rtc (RTSP -> WebRTC), gevoed door de avent-webrtc-bridge.

Keten:  Philips-cloud -> bridge (addon, RTSP) -> go2rtc (WebRTC) -> WebView2

Herbruikt alle bewezen login/discovery/DPAPI-logica uit babyfoon_app.py.
"""

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import webview

INSTANCE_PORT = 49517           # single-instance-slot (voorkomt WebView2-vastloper)
_INSTANCE_LOCK = None           # blijft leven zolang het proces draait

from babyfoon_app import (
    TuyaClient, TuyaAPIError,
    TUYA_SIGNING_KEY, TUYA_APP_KEY, TUYA_PACKAGE_NAME, DEFAULT_PORT,
    ROOT, BRIDGE_EXE, BRIDGE_CONFIG_PATH, TUYA_DATA,
    dpapi_encrypt, dpapi_decrypt, load_config, save_config,
    cam_id, cam_name, port_open, app_dir,
)

def res_dir() -> Path:
    """Map met READ-ONLY assets (ui, go2rtc, bridge). In een PyInstaller-onefile
    zijn die uitgepakt naar sys._MEIPASS; los draaien = naast het script."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return app_dir()


# READ-ONLY (gebundeld) ------------------------------------------------------
GO2RTC_EXE = res_dir() / "bin" / "go2rtc.exe"
BRIDGE_EXE_RES = res_dir() / "repo" / "avent-webrtc-bridge" / "avent-webrtc-bridge.exe"
UI_FILE = res_dir() / "ui" / "index.html"
# SCHRIJFBAAR (naast de exe) — app_dir() geeft de exe-map bij een frozen build --
GO2RTC_CONFIG = app_dir() / "go2rtc.yaml"
GO2RTC_API_PORT = 1984          # go2rtc web/API + WebRTC signaling
LOG_PATH = app_dir() / "peekababy.log"


def _log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _kill_orphans():
    """Stop go2rtc/bridge die een vorige (gecrashte) run heeft achtergelaten,
    zodat poorten vrij zijn en er geen stale go2rtc met oude config draait."""
    for name in ("go2rtc.exe", "avent-webrtc-bridge.exe"):
        try:
            subprocess.run(["taskkill", "/F", "/IM", name],
                           capture_output=True, creationflags=0x08000000)
        except Exception:
            pass
    # Alleen ONZE eigen achtergebleven WebView2-processen (herkenbaar aan onze
    # unieke profielmap-prefix) opruimen -> voorkomt de opstart-vastloper.
    # Raakt WebView2 van andere apps niet.
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | "
             "Where-Object { $_.CommandLine -like '*peekababy-wv-*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
            capture_output=True, creationflags=0x08000000, timeout=20)
    except Exception:
        pass


def _acquire_single_instance():
    """True als wij de enige instantie zijn; False als er al een draait."""
    global _INSTANCE_LOCK
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", INSTANCE_PORT))
        s.listen(1)
    except OSError:
        return False
    _INSTANCE_LOCK = s
    return True


class Api:
    """Wordt vanuit de JS aangeroepen via window.pywebview.api.*"""

    def __init__(self):
        self.cfg = load_config()
        self.window = None
        self._pending = None            # login-in-uitvoering (tussen signIn en code)
        self.bridge_proc = None
        self.go2rtc_proc = None
        self._blog = None
        self._glog = None

    # -- helpers -----------------------------------------------------------
    def _client(self, sid=""):
        device_id = self.cfg.get("device_id") or uuid.uuid4().hex[:40]
        self.cfg["device_id"] = device_id
        return TuyaClient(TUYA_SIGNING_KEY, TUYA_APP_KEY, device_id, sid=sid)

    def _cameras_out(self):
        return [{"id": c["id"], "name": c["name"], "online": True}
                for c in self.cfg.get("cameras", [])]

    def _store_login(self, result, email, country, password, remember):
        self.cfg["email"] = email
        self.cfg["country"] = country
        self.cfg["session"] = {
            "sid": result.get("sid", ""),
            "ecode": result.get("ecode", ""),
            "partner": result.get("partnerIdentity", ""),
        }
        if remember:
            try:
                self.cfg["password_enc"] = dpapi_encrypt(password)
            except Exception:
                self.cfg.pop("password_enc", None)
        save_config(self.cfg)

    def _discover_and_store(self, client):
        cams = client.discover_cameras()
        if cams:
            self.cfg["cameras"] = [
                {"id": cam_id(d), "name": cam_name(d), "product_id": d.get("productId", "")}
                for d in cams
            ]
            save_config(self.cfg)
        return cams

    # -- door de UI aangeroepen -------------------------------------------
    def bootstrap(self):
        """Beginstatus voor de UI: is er al een bekende sessie?"""
        try:
            user = None
            return {
                "signedIn": bool(self.cfg.get("cameras") and self.cfg.get("session", {}).get("sid")),
                "email": self.cfg.get("email", ""),
                "nickname": self.cfg.get("nickname", ""),
                "lang": self.cfg.get("lang", "en"),
                "cameras": self._cameras_out(),
            }
        except Exception as e:
            return {"error": str(e)}

    def signIn(self, email, password, country, remember):
        email = (email or "").strip()
        country = (country or "31").strip() or "31"
        if not email or not password:
            return {"error": "Vul e-mail en wachtwoord in."}
        client = self._client()
        try:
            result = client.login_full(email, password, country, mfa_code="")
        except TuyaAPIError as e:
            if e.code == "MFA_NEED_SEND_CODE":
                self._pending = {"client": client, "email": email, "password": password,
                                 "country": country, "remember": remember}
                try:
                    client.trigger_mfa(email, password, country)
                except TuyaAPIError as te:
                    return {"error": f"MFA aanvragen mislukt: {te.message}"}
                return {"mfa": True, "email": email}
            if "USER_NOT_EXIST" in e.code or "NOT_EXISTS" in e.code:
                return {"error": "Onbekend account. Klopt de landcode? (31 NL · 32 BE · 39 IT)"}
            if "PASSWD" in e.code or "PASSWORD" in e.code:
                return {"error": "Wachtwoord onjuist."}
            return {"error": e.message}
        except Exception as e:
            return {"error": f"Netwerkfout: {e}"}
        # Login zonder MFA gelukt (vertrouwd apparaat)
        return self._finish_login(client, result, email, country, password, remember)

    def submitMfaCode(self, code):
        p = self._pending
        if not p:
            return {"error": "Geen actieve login. Begin opnieuw."}
        code = (code or "").strip().replace(" ", "")
        if not (code.isdigit() and len(code) == 6):
            return {"error": "Voer precies 6 cijfers in."}
        try:
            result = p["client"].login_full(p["email"], p["password"], p["country"], mfa_code=code)
        except TuyaAPIError as e:
            if "MFA" in e.code:
                return {"error": "Code afgekeurd. Probeer de nieuwste code."}
            return {"error": e.message}
        except Exception as e:
            return {"error": f"Netwerkfout: {e}"}
        return self._finish_login(p["client"], result, p["email"], p["country"],
                                  p["password"], p["remember"])

    def _finish_login(self, client, result, email, country, password, remember):
        try:
            user = client.get_user_info() or {}
        except Exception:
            user = {}
        self.cfg["nickname"] = user.get("nickname", "")
        self._store_login(result, email, country, password, remember)
        cams = self._discover_and_store(client)
        self._pending = None
        if not cams:
            return {"ok": True, "cameras": [], "nickname": self.cfg.get("nickname", ""),
                    "warning": "Geen camera's gevonden. Staat de monitor online in de app?"}
        return {"ok": True, "cameras": self._cameras_out(),
                "nickname": self.cfg.get("nickname", "")}

    def resendMfaCode(self):
        p = self._pending
        if not p:
            return {"error": "Geen actieve login."}
        try:
            p["client"].trigger_mfa(p["email"], p["password"], p["country"])
            return {"ok": True}
        except TuyaAPIError as e:
            return {"error": e.message}

    def listCameras(self):
        res = self._ensure_session()
        if isinstance(res, dict) and res.get("error"):
            return self._cameras_out()   # val terug op onthouden lijst
        client = res
        try:
            self._discover_and_store(client)
        except Exception:
            pass
        return self._cameras_out()

    def _ensure_session(self):
        """Geef een client met geldige sessie, of {'error':...}."""
        sess = self.cfg.get("session", {})
        if sess.get("sid"):
            client = self._client(sid=sess["sid"])
            try:
                client.get_user_info()
                return client
            except Exception:
                pass
        # herinloggen met opgeslagen wachtwoord
        email = self.cfg.get("email", "")
        country = self.cfg.get("country", "31")
        if not self.cfg.get("password_enc"):
            return {"error": "reauth", "reason": "no-password"}
        try:
            password = dpapi_decrypt(self.cfg["password_enc"])
        except Exception:
            return {"error": "reauth", "reason": "decrypt"}
        client = self._client()
        try:
            result = client.login_full(email, password, country, mfa_code="")
        except TuyaAPIError as e:
            if e.code == "MFA_NEED_SEND_CODE":
                self._pending = {"client": client, "email": email, "password": password,
                                 "country": country, "remember": True}
                try:
                    client.trigger_mfa(email, password, country)
                except Exception:
                    pass
                return {"error": "reauth", "reason": "mfa"}
            return {"error": e.message}
        except Exception as e:
            return {"error": str(e)}
        self.cfg["session"] = {"sid": result.get("sid", ""), "ecode": result.get("ecode", ""),
                               "partner": result.get("partnerIdentity", "")}
        save_config(self.cfg)
        return client

    def watch(self, ids):
        """Start bridge (addon) + go2rtc voor de gekozen camera's. Geeft streams terug."""
        chosen = [c for c in self.cfg.get("cameras", []) if c["id"] in set(ids or [])]
        if not chosen:
            return {"error": "Geen camera's gekozen."}
        if not BRIDGE_EXE_RES.exists():
            return {"error": f"Bridge niet gevonden: {BRIDGE_EXE_RES}"}
        if not GO2RTC_EXE.exists():
            return {"error": f"go2rtc niet gevonden: {GO2RTC_EXE}"}

        self.cfg["selected"] = [c["id"] for c in chosen]
        save_config(self.cfg)

        res = self._ensure_session()
        if isinstance(res, dict):
            return res   # bevat 'error'
        sess = self.cfg["session"]
        port = int(self.cfg.get("port", DEFAULT_PORT))

        bridge_cfg = {
            "signing_key": TUYA_SIGNING_KEY, "sid": sess["sid"], "ecode": sess["ecode"],
            "partner": sess["partner"], "app_key": TUYA_APP_KEY,
            "device_id": self.cfg["device_id"], "package_name": TUYA_PACKAGE_NAME,
            "bridge_port": port,
            "cameras": [{"camera_id": c["id"], "camera_name": c["name"],
                         "product_id": c.get("product_id", "")} for c in chosen],
        }
        BRIDGE_CONFIG_PATH.write_text(json.dumps(bridge_cfg, indent=2), encoding="utf-8")

        self._stop_procs()
        NO_WINDOW = 0x08000000
        self._blog = open(app_dir() / "bridge.log", "ab", buffering=0)
        _log(f"start bridge (port {port}) cams={[c['id'] for c in chosen]}")
        self.bridge_proc = subprocess.Popen(
            [str(BRIDGE_EXE_RES), "addon", "--config", str(BRIDGE_CONFIG_PATH)],
            cwd=str(ROOT), creationflags=NO_WINDOW, stdout=self._blog, stderr=self._blog)

        # wachten tot RTSP-poort open is
        deadline = time.time() + 25
        while time.time() < deadline:
            if self.bridge_proc.poll() is not None:
                return {"error": "Bridge stopte voortijdig. Sessie mogelijk verlopen — log opnieuw in."}
            if port_open(port):
                break
            time.sleep(0.4)
        else:
            return {"error": f"RTSP-poort {port} kwam niet online."}

        _log(f"RTSP port {port} open; bridge alive={self.bridge_proc.poll() is None}")
        paths = self._read_rtsp_paths()
        streams = {}
        for c in chosen:
            path = paths.get(c["id"]) or ("/" + c["name"].replace(" ", "_"))
            streams[c["id"]] = f"rtsp://127.0.0.1:{port}{path}"
        self._write_go2rtc_config(streams)
        self._glog = open(app_dir() / "go2rtc.log", "ab", buffering=0)
        self.go2rtc_proc = subprocess.Popen(
            [str(GO2RTC_EXE), "-config", str(GO2RTC_CONFIG)],
            cwd=str(app_dir()), creationflags=NO_WINDOW, stdout=self._glog, stderr=self._glog)
        # wachten tot go2rtc-API leeft
        d2 = time.time() + 12
        while time.time() < d2 and not port_open(GO2RTC_API_PORT):
            time.sleep(0.3)
        _log(f"go2rtc api {GO2RTC_API_PORT} open={port_open(GO2RTC_API_PORT)} alive={self.go2rtc_proc.poll() is None}")

        return {"ok": True, "api": GO2RTC_API_PORT,
                "streams": [{"id": c["id"], "streamId": c["id"]} for c in chosen]}

    def stop(self):
        self._stop_procs()
        return {"ok": True}

    def stopOne(self, cam_id_):
        # go2rtc streamt lui: als geen client meer kijkt stopt de stream vanzelf.
        return {"ok": True}

    def popOut(self, cam_id_):
        cam = next((c for c in self.cfg.get("cameras", []) if c["id"] == cam_id_), None)
        if not cam:
            return {"error": "Onbekende camera."}
        url = f"http://127.0.0.1:{GO2RTC_API_PORT}/webrtc.html?src={cam_id_}"
        try:
            webview.create_window(cam["name"], url, width=420, height=300,
                                  on_top=True, frameless=False)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def refreshCameras(self):
        return {"cameras": self.listCameras()}

    def signOut(self):
        self._stop_procs()
        for k in ("session", "password_enc", "cameras", "selected", "nickname"):
            self.cfg.pop(k, None)
        save_config(self.cfg)
        return {"ok": True}

    def setTheme(self, name):
        self.cfg["theme"] = name
        save_config(self.cfg)
        return {"ok": True}

    def setLang(self, lang):
        self.cfg["lang"] = "nl" if lang == "nl" else "en"
        save_config(self.cfg)
        return {"ok": True}

    # -- Camerabediening (DPS: nachtlampje, slaapliedjes, volume) ---------
    def _set_dps(self, cam_id, dps):
        res = self._ensure_session()
        if isinstance(res, dict):
            _log(f"set_dps SKIP (geen sessie) {dps}")
            return res
        try:
            r = res.set_dps(cam_id, dps)
            _log(f"set_dps OK {cam_id} {dps} -> {r}")
            return {"ok": True}
        except Exception as e:
            _log(f"set_dps ERR {cam_id} {dps} -> {e}")
            return {"error": str(e)}

    def camControls(self, cam_id):
        """Huidige stand van nachtlampje/volume/slaapliedje voor het paneel."""
        res = self._ensure_session()
        if isinstance(res, dict):
            return res
        try:
            dev = res.get_device(cam_id) or {}
        except Exception as e:
            return {"error": str(e)}
        dps = dev.get("dps", {}) or {}
        track = None
        try:
            if dps.get("248"):
                track = json.loads(dps["248"]).get("id")
        except Exception:
            track = None

        def _int(v, d):
            try:
                return int(v)
            except Exception:
                return d

        return {
            "nightlight": bool(dps.get("138")),
            "brightness": _int(dps.get("158"), 50),
            "volume": _int(dps.get("209"), 50),
            "trackId": track,
            "playMode": dps.get("203") or "loop",
            "privacy": dps.get("237") == "1",
        }

    def roomTemp(self, cam_id):
        """Kamertemperatuur (DPS 207 / 100 => graden Celsius)."""
        res = self._ensure_session()
        if isinstance(res, dict):
            return {"temp": None}
        try:
            dev = res.get_device(cam_id) or {}
        except Exception:
            return {"temp": None}
        t = (dev.get("dps", {}) or {}).get("207")
        try:
            return {"temp": round(int(t) / 100.0, 1)}
        except (TypeError, ValueError):
            return {"temp": None}

    def setNightlight(self, cam_id, on):
        return self._set_dps(cam_id, {"138": bool(on)})

    def setBrightness(self, cam_id, val):
        return self._set_dps(cam_id, {"158": int(val)})

    def setVolume(self, cam_id, val):
        return self._set_dps(cam_id, {"209": int(val)})

    def setPlayMode(self, cam_id, mode):
        if mode not in ("loop", "loop1", "shuffle"):
            return {"error": "onbekende modus"}
        return self._set_dps(cam_id, {"203": mode})

    def setPrivacy(self, cam_id, on):
        return self._set_dps(cam_id, {"237": "1" if on else "0"})

    def lullabyControl(self, cam_id, action):
        if action not in ("play", "pause", "stop", "next", "prev"):
            return {"error": "onbekende actie"}
        return self._set_dps(cam_id, {"201": action})

    def playLullaby(self, cam_id, track_id):
        return self._set_dps(cam_id, {
            "202": json.dumps({"bizcode": "phi-no-bm", "id": int(track_id)}),
            "201": "play",
        })

    # -- intern -----------------------------------------------------------
    def _read_rtsp_paths(self):
        out = {}
        try:
            data = json.loads((TUYA_DATA / "cameras.json").read_text(encoding="utf-8"))
            for c in data.get("cameras", []):
                if c.get("deviceId") and c.get("rtspPath"):
                    out[c["deviceId"]] = c["rtspPath"]
        except Exception:
            pass
        return out

    def _write_go2rtc_config(self, streams):
        lines = [
            "api:",
            f"  listen: \"127.0.0.1:{GO2RTC_API_PORT}\"",
            "  origin: \"*\"",          # sta de WebSocket vanuit het app-venster toe
            "",
            "rtsp:",
            "  listen: \"\"",            # geen eigen RTSP-server (bridge gebruikt 8554)
            "",
            "webrtc:",
            "  listen: \":8555\"",
            "  candidates:",
            "    - 127.0.0.1:8555",
            "",
            "log:", "  level: info", "",
            "streams:",
        ]
        for sid, url in streams.items():
            # #media=video+audio dwingt go2rtc de juiste sporen te onderhandelen
            lines.append(f"  {sid}: {url}")
        GO2RTC_CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _log(f"go2rtc streams: {streams}")

    def log(self, msg):
        """Door de JS aangeroepen om diagnostiek naar peekababy.log te schrijven."""
        _log(f"[js] {msg}")
        return True

    def _stop_procs(self):
        for p in (self.go2rtc_proc, self.bridge_proc):
            try:
                if p and p.poll() is None:
                    p.terminate()
            except Exception:
                pass
        for f in (self._glog, self._blog):
            try:
                if f:
                    f.close()
            except Exception:
                pass
        self._glog = None
        self._blog = None
        self.go2rtc_proc = None
        self.bridge_proc = None


def main():
    if not _acquire_single_instance():
        _log("another instance already running -> exit")
        return
    _kill_orphans()
    api = Api()
    theme = api.cfg.get("theme")
    import os
    debug = os.environ.get("PEEKABABY_DEBUG") == "1"
    _log(f"=== PeekaBaby start (debug={debug}) UI={UI_FILE} ===")
    window = webview.create_window(
        "PeekaBaby", str(UI_FILE), js_api=api,
        width=1040, height=700, min_size=(720, 520),
        background_color="#14121C")
    api.window = window

    # Smoke-test-modus: open en sluit automatisch (voor geautomatiseerde controle).
    import os
    if os.environ.get("PEEKABABY_SMOKE") == "1":
        def _close():
            time.sleep(3.5)
            try:
                window.destroy()
            except Exception:
                pass
        threading.Thread(target=_close, daemon=True).start()

    # Eigen, unieke WebView2-profielmap per start -> geen lock-botsing met
    # achtergebleven webview-processen (voorkomt wit/niet-reagerend venster).
    wv_dir = tempfile.mkdtemp(prefix="peekababy-wv-")

    def _cleanup():
        api._stop_procs()
        shutil.rmtree(wv_dir, ignore_errors=True)

    window.events.closed += _cleanup
    # private_mode=False zodat storage_path ECHT wordt gebruikt (bij True negeert
    # pywebview 'm en delen alle starts dezelfde profielmap -> lock-vastloper).
    webview.start(debug=debug, private_mode=False, storage_path=wv_dir)


if __name__ == "__main__":
    main()
