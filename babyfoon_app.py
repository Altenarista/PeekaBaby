#!/usr/bin/env python3
"""
Babyfoon — grafische app voor de Philips Avent babyfoon (Windows).

Alles-in-een venster:
  * Inloggen op de Philips Avent (Tuya) cloud (e-mail + wachtwoord + MFA).
  * Alle camera's in je account ontdekken en als aanvinklijst tonen.
  * Per aangevinkte camera een eigen VLC pop-out (altijd bovenop) openen.
  * Onthoudt na 1x inloggen alles: e-mail, landcode, device-id, je
    (versleutelde) wachtwoord en de sessie. Bij een verlopen sessie logt de
    app automatisch opnieuw in — meestal zonder nieuwe MFA-code, omdat het
    apparaat vertrouwd is.

Draait als losse .exe (PyInstaller) zodat er geen Python nodig is op de pc.
VLC moet wel geinstalleerd zijn (https://www.videolan.org).
"""

import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


# ---------------------------------------------------------------------------
# Paden
# ---------------------------------------------------------------------------

def app_dir() -> Path:
    """Map waarin de app draait (naast de .exe, of naast dit script)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_dir()
CONFIG_PATH = ROOT / "babyfoon_config.json"
BRIDGE_CONFIG_PATH = ROOT / "babyfoon_bridge.json"
BRIDGE_EXE = ROOT / "repo" / "avent-webrtc-bridge" / "avent-webrtc-bridge.exe"
TUYA_DATA = ROOT / ".tuya-data"
DEFAULT_PORT = 8554

# Statische APK-credentials (uit custom_components/philips_avent/const.py).
TUYA_SIGNING_KEY = (
    "com.philips.ph.babymonitorplus"
    "_D2:D6:95:A1:1D:1B:84:F9:25:A9:45:6E:27:F4:45:E9:FD:87:C3:74"
    ":63:AA:8A:34:32:A6:6A:23:3B:0F:D5:0F"
    "_8n459nxk9g98gqgcwrpk3csv97uuwajm"
    "_a3nfht4ufwfw9cmkspaftv4x89cx58qx"
)
TUYA_APP_KEY = "wx3at9qprkhskvkcsyhm"
TUYA_PACKAGE_NAME = "com.philips.ph.babymonitorplus"


# ---------------------------------------------------------------------------
# Wachtwoord veilig opslaan met Windows DPAPI (alleen dit Windows-account leest)
# ---------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_encrypt(plaintext: str) -> str:
    """Versleutel een string met DPAPI; geef base64-tekst terug."""
    blob_in = _blob(plaintext.encode("utf-8"))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return base64.b64encode(raw).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_decrypt(b64: str) -> str:
    """Ontsleutel een DPAPI-base64 string terug naar platte tekst."""
    raw = base64.b64decode(b64)
    blob_in = _blob(raw)
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# ---------------------------------------------------------------------------
# Tuya-client (compacte, ingebouwde versie van repo/examples/tuya_client.py)
# ---------------------------------------------------------------------------

SIGN_PARAM_WHITELIST = frozenset([
    "a", "v", "lat", "lon", "lang", "deviceId", "appVersion", "ttid",
    "isH5", "h5Token", "os", "clientId", "postData", "time", "requestId",
    "et", "n4h5", "sid", "chKey", "sp",
])


def _swap_sign_string(s: str) -> str:
    if len(s) != 32:
        return s
    return s[8:16] + s[0:8] + s[24:32] + s[16:24]


def _compute_sign(params, signing_key):
    filtered = {k: v for k, v in params.items() if k in SIGN_PARAM_WHITELIST and v}
    if filtered.get("postData"):
        md5 = hashlib.md5(filtered["postData"].encode()).hexdigest()
        filtered["postData"] = _swap_sign_string(md5)
    param_str = "||".join(f"{k}={filtered[k]}" for k in sorted(filtered))
    return hmac.new(signing_key.encode(), param_str.encode(), hashlib.sha256).hexdigest()


class TuyaAPIError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class TuyaClient:
    base_url = "https://a1.tuyaeu.com/api.json"
    app_version = "1.8.0"
    sdk_version = "6.7.0"

    def __init__(self, signing_key, app_key, device_id, sid=""):
        self.signing_key = signing_key
        self.app_key = app_key
        self.device_id = device_id
        self.ch_key = "071d81fa"
        self.sid = sid

    def _build_params(self, action, version="1.0", post_data=None):
        params = {
            "a": action, "v": version, "time": str(int(time.time())),
            "appVersion": self.app_version, "appRnVersion": "5.92",
            "channel": "oem", "chKey": self.ch_key, "clientId": self.app_key,
            "cp": "gzip", "deviceCoreVersion": self.sdk_version,
            "deviceId": self.device_id, "et": "0.0.1", "nd": "1",
            "lang": "en_US", "os": "Android", "osSystem": "14",
            "platform": "tuya_client", "requestId": str(uuid.uuid4()),
            "sdkVersion": self.sdk_version, "sid": self.sid,
            "timeZoneId": "Europe/Rome", "ttid": f"sdk_international@{self.app_key}",
        }
        if post_data is not None:
            params["postData"] = json.dumps(post_data) if not isinstance(post_data, str) else post_data
        params["sign"] = _compute_sign(params, self.signing_key)
        return params

    def call(self, action, version="1.0", post_data=None, extra_params=None):
        params = self._build_params(action, version, post_data)
        if extra_params:
            params.update(extra_params)
        r = requests.post(
            self.base_url, data=params,
            headers={
                "User-Agent": f"Thing-UA=APP/Android/{self.app_version}/SDK/{self.sdk_version}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15,
        )
        resp = r.json()
        if not resp.get("success"):
            raise TuyaAPIError(resp.get("errorCode", "UNKNOWN"),
                               resp.get("errorMsg", "Unknown error"))
        return resp

    def call_result(self, action, version="1.0", post_data=None, extra_params=None):
        return self.call(action, version, post_data, extra_params).get("result")

    # -- Auth --
    def _get_rsa_token(self, email, country_code):
        return self.call_result("thing.m.user.username.token.get", "2.0",
                                 {"countryCode": country_code, "username": email, "isUid": False})

    def _encrypt_password(self, password, pb_key):
        md5_pass = hashlib.md5(password.encode()).hexdigest()
        pem = f"-----BEGIN PUBLIC KEY-----\n{pb_key}\n-----END PUBLIC KEY-----"
        cipher = PKCS1_v1_5.new(RSA.import_key(pem))
        return cipher.encrypt(md5_pass.encode()).hex()

    def login_full(self, email, password, country_code, mfa_code=""):
        """Login; geef het VOLLEDIGE result terug (sid + ecode + partnerIdentity)."""
        token_data = self._get_rsa_token(email, country_code)
        encrypted = self._encrypt_password(password, token_data["pbKey"])
        old_sid = self.sid
        self.sid = ""
        try:
            result = self.call_result(
                "thing.m.user.email.password.login", "3.0",
                {"countryCode": country_code, "email": email, "passwd": encrypted,
                 "token": token_data["token"], "ifencrypt": 1,
                 "options": json.dumps({"group": 1, "mfaCode": mfa_code})},
            )
            self.sid = result["sid"]
            return result
        except TuyaAPIError:
            self.sid = old_sid
            raise

    def trigger_mfa(self, email, password, country_code):
        token_data = self._get_rsa_token(email, country_code)
        encrypted = self._encrypt_password(password, token_data["pbKey"])
        old_sid = self.sid
        self.sid = ""
        try:
            return self.call_result(
                "thing.m.user.username.mfa.code.get", "1.0",
                {"countryCode": country_code, "username": email, "passwd": encrypted,
                 "token": token_data["token"], "ifencrypt": 1,
                 "options": json.dumps({"group": 1, "mfaCode": "null"})},
            )
        finally:
            self.sid = old_sid

    def get_user_info(self):
        return self.call_result("smartlife.m.user.info.get")

    def get_homes(self):
        return self.call_result("m.life.home.space.list")

    # -- Apparaatstatus + bediening (DPS) --
    def get_device(self, dev_id):
        """Volledige apparaatinfo incl. huidige DPS-waarden."""
        return self.call_result("tuya.m.device.get", post_data={"devId": dev_id})

    def set_dps(self, dev_id, dps):
        """Zet één of meer DPS-waarden (nachtlampje, volume, slaapliedje, …).
        Versie "1.0" — zo doet de werkende HA-integratie (api.py) het ook."""
        return self.call_result(
            "tuya.m.device.dp.publish", "1.0",
            {"devId": dev_id, "gwId": dev_id, "dps": dps},
        )

    # -- Camera-ontdekking (poort-strategieen uit avent_setup.py) --
    def discover_cameras(self):
        cameras, seen = [], set()

        def add(dev):
            dev_id = dev.get("devId") or dev.get("deviceId") or dev.get("id")
            if dev_id and dev_id not in seen:
                seen.add(dev_id)
                cameras.append(dev)

        try:
            homes = self.get_homes() or []
        except TuyaAPIError:
            homes = []

        for home in homes:
            gid = str(home.get("gid", ""))
            if not gid:
                continue
            for ver in ("2.0", "1.0"):
                try:
                    rooms = self.call_result("tuya.m.location.get", ver,
                                             post_data={"gid": gid}, extra_params={"gid": gid})
                    if isinstance(rooms, list):
                        for room in rooms:
                            for dev in room.get("deviceList", []):
                                add(dev)
                    if cameras:
                        break
                except TuyaAPIError:
                    pass
            if not cameras:
                try:
                    res = self.call_result("tuya.m.my.group.device.list", extra_params={"gid": gid})
                    if isinstance(res, list):
                        for dev in res:
                            add(dev)
                except TuyaAPIError:
                    pass
        if cameras:
            return cameras
        for home in homes:
            gid = str(home.get("gid", ""))
            if not gid:
                continue
            try:
                res = self.call_result("tuya.m.my.group.device.relation.list", extra_params={"gid": gid})
                if isinstance(res, list):
                    for dev in res:
                        add(dev)
            except TuyaAPIError:
                pass
        if cameras:
            return cameras
        try:
            res = self.call_result("tuya.m.device.list.get")
            if isinstance(res, list):
                for dev in res:
                    add(dev)
        except TuyaAPIError:
            pass
        return cameras


def cam_name(d):
    return d.get("name") or d.get("deviceName") or "(naamloos)"


def cam_id(d):
    return d.get("devId") or d.get("deviceId") or d.get("id")


# ---------------------------------------------------------------------------
# Config (onthouden)
# ---------------------------------------------------------------------------

def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def find_vlc():
    for p in (r"C:\Program Files\VideoLAN\VLC\vlc.exe",
              r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"):
        if os.path.exists(p):
            return p
    return None


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

APP_NAME = "PeekaBaby"
COUNTRY_HINT = "31 = NL, 32 = BE, 39 = IT"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW (bridge zonder console-venster)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} — Philips Avent babyfoon")
        self.geometry("460x520")
        self.minsize(420, 480)
        self.configure(padx=0, pady=0)

        self.cfg = load_config()
        self.bridge_proc = None
        self.vlc_procs = []
        self.cam_vars = {}  # camera_id -> tk.BooleanVar

        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        self.container = ttk.Frame(self, padding=16)
        self.container.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="")
        status = ttk.Label(self, textvariable=self.status_var, anchor="w",
                           relief="sunken", padding=(8, 4))
        status.pack(side="bottom", fill="x")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Startscherm bepalen: camera's bekend -> direct kijkscherm.
        if self.cfg.get("cameras"):
            self.show_cameras()
        else:
            self.show_login()

    # -- helpers -----------------------------------------------------------
    def status(self, msg):
        self.status_var.set(msg)
        self.update_idletasks()

    def clear(self):
        for w in self.container.winfo_children():
            w.destroy()

    def make_client(self, sid=""):
        device_id = self.cfg.get("device_id") or uuid.uuid4().hex[:40]
        self.cfg["device_id"] = device_id
        return TuyaClient(TUYA_SIGNING_KEY, TUYA_APP_KEY, device_id, sid=sid)

    # -- login -------------------------------------------------------------
    def show_login(self):
        self.clear()
        f = self.container
        ttk.Label(f, text=APP_NAME, font=("Segoe UI Semibold", 20)).pack(anchor="w")
        ttk.Label(f, text="Log in met je Baby Monitor+ account",
                  foreground="#666").pack(anchor="w", pady=(0, 16))

        ttk.Label(f, text="E-mail").pack(anchor="w")
        self.email_var = tk.StringVar(value=self.cfg.get("email", ""))
        ttk.Entry(f, textvariable=self.email_var, width=40).pack(fill="x", pady=(0, 10))

        ttk.Label(f, text="Wachtwoord").pack(anchor="w")
        self.pw_var = tk.StringVar()
        pw = ttk.Entry(f, textvariable=self.pw_var, show="•", width=40)
        pw.pack(fill="x", pady=(0, 10))

        ttk.Label(f, text=f"Landcode ({COUNTRY_HINT})").pack(anchor="w")
        self.country_var = tk.StringVar(value=self.cfg.get("country", "31"))
        ttk.Entry(f, textvariable=self.country_var, width=10).pack(anchor="w", pady=(0, 12))

        self.remember_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Wachtwoord veilig onthouden (auto herinloggen)",
                        variable=self.remember_var).pack(anchor="w", pady=(0, 16))

        self.login_btn = ttk.Button(f, text="Inloggen", command=self.do_login)
        self.login_btn.pack(anchor="w")
        self.bind("<Return>", lambda e: self.do_login())
        pw.focus_set()

    def _ask_mfa_and_login(self, client, email, password, country):
        """Vraag MFA-code (met 'nieuwe code' optie) en log in. Return result of None."""
        self.status("MFA-code aangevraagd — check je e-mail…")
        try:
            client.trigger_mfa(email, password, country)
        except TuyaAPIError as e:
            messagebox.showerror(APP_NAME, f"MFA-code aanvragen mislukt:\n{e}")
            return None
        for _ in range(5):
            code = simpledialog.askstring(
                APP_NAME,
                "Voer de 6-cijferige code uit je e-mail in.\n"
                "(laat leeg + OK voor een nieuwe code)",
                parent=self)
            if code is None:
                return None
            code = code.strip().replace(" ", "")
            if code == "":
                self.status("Nieuwe MFA-code aanvragen…")
                try:
                    client.trigger_mfa(email, password, country)
                except TuyaAPIError:
                    pass
                continue
            if not (code.isdigit() and len(code) == 6):
                messagebox.showwarning(APP_NAME, "Voer precies 6 cijfers in.")
                continue
            self.status("Inloggen met code…")
            try:
                return client.login_full(email, password, country, mfa_code=code)
            except TuyaAPIError as e:
                if "MFA" in e.code:
                    messagebox.showwarning(APP_NAME, f"Code afgekeurd ({e.code}). Probeer de nieuwste.")
                    continue
                messagebox.showerror(APP_NAME, str(e))
                return None
        return None

    def _perform_login(self, client, email, password, country):
        """Login zonder MFA proberen; anders MFA-flow. Return result dict of None."""
        self.status("Inloggen…")
        try:
            return client.login_full(email, password, country, mfa_code="")
        except TuyaAPIError as e:
            if e.code == "MFA_NEED_SEND_CODE":
                return self._ask_mfa_and_login(client, email, password, country)
            if "USER_NOT_EXIST" in e.code or "NOT_EXISTS" in e.code:
                messagebox.showerror(APP_NAME, "Onbekend account. Klopt de landcode?\n"
                                     f"({COUNTRY_HINT})")
            elif "PASSWD" in e.code or "PASSWORD" in e.code:
                messagebox.showerror(APP_NAME, "Wachtwoord onjuist.")
            else:
                messagebox.showerror(APP_NAME, str(e))
            return None

    def do_login(self):
        email = self.email_var.get().strip()
        password = self.pw_var.get()
        country = (self.country_var.get().strip() or "31")
        if not email or not password:
            messagebox.showwarning(APP_NAME, "Vul e-mail en wachtwoord in.")
            return
        self.login_btn.config(state="disabled")
        try:
            client = self.make_client()
            result = self._perform_login(client, email, password, country)
            if not result:
                return
            self.status("Ingelogd — camera's zoeken…")
            try:
                user = client.get_user_info() or {}
            except TuyaAPIError:
                user = {}
            cams = client.discover_cameras()
            if not cams:
                messagebox.showerror(APP_NAME, "Geen camera's gevonden.\n"
                                     "Staat de monitor online in de Baby Monitor+ app?")
                return
            # Onthouden
            self.cfg["email"] = email
            self.cfg["country"] = country
            self.cfg["nickname"] = user.get("nickname", "")
            self.cfg["session"] = {
                "sid": result.get("sid", ""),
                "ecode": result.get("ecode", ""),
                "partner": result.get("partnerIdentity", ""),
            }
            if self.remember_var.get():
                try:
                    self.cfg["password_enc"] = dpapi_encrypt(password)
                except Exception:
                    self.cfg.pop("password_enc", None)
            else:
                self.cfg.pop("password_enc", None)
            self.cfg["cameras"] = [
                {"id": cam_id(d), "name": cam_name(d), "product_id": d.get("productId", "")}
                for d in cams
            ]
            self.cfg.setdefault("port", DEFAULT_PORT)
            save_config(self.cfg)
            self.status(f"Ingelogd als {user.get('nickname', email)}")
            self.show_cameras()
        except requests.RequestException as e:
            messagebox.showerror(APP_NAME, f"Netwerkfout:\n{e}")
        finally:
            try:
                self.login_btn.config(state="normal")
            except tk.TclError:
                pass

    # -- sessie geldig houden ---------------------------------------------
    def ensure_session(self):
        """Geef een client met geldige sessie terug, of None bij falen."""
        sess = self.cfg.get("session", {})
        client = self.make_client(sid=sess.get("sid", ""))
        client_ecode = sess.get("ecode", "")
        client_partner = sess.get("partner", "")
        if sess.get("sid"):
            self.status("Sessie controleren…")
            try:
                client.get_user_info()
                return client, client_ecode, client_partner
            except (TuyaAPIError, requests.RequestException):
                pass  # verlopen -> herinloggen
        # Herinloggen
        email = self.cfg.get("email", "")
        country = self.cfg.get("country", "31")
        password = None
        if self.cfg.get("password_enc"):
            try:
                password = dpapi_decrypt(self.cfg["password_enc"])
            except Exception:
                password = None
        if password is None:
            password = simpledialog.askstring(
                APP_NAME, f"Sessie verlopen. Voer je wachtwoord in voor {email}:",
                show="•", parent=self)
            if not password:
                return None
        client = self.make_client()
        result = self._perform_login(client, email, password, country)
        if not result:
            return None
        self.cfg["session"] = {
            "sid": result.get("sid", ""),
            "ecode": result.get("ecode", ""),
            "partner": result.get("partnerIdentity", ""),
        }
        save_config(self.cfg)
        return client, result.get("ecode", ""), result.get("partnerIdentity", "")

    # -- camerascherm ------------------------------------------------------
    def show_cameras(self):
        self.clear()
        f = self.container
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text=APP_NAME, font=("Segoe UI Semibold", 18)).pack(side="left")
        who = self.cfg.get("nickname") or self.cfg.get("email", "")
        ttk.Label(top, text=who, foreground="#666").pack(side="right", pady=(6, 0))

        ttk.Label(f, text="Kies welke camera's je wilt bekijken:",
                  foreground="#666").pack(anchor="w", pady=(10, 8))

        listbox = ttk.Frame(f, relief="groove", borderwidth=1, padding=8)
        listbox.pack(fill="both", expand=True)

        selected = set(self.cfg.get("selected", []))
        cams = self.cfg.get("cameras", [])
        if not selected and cams:
            selected = {cams[0]["id"]}  # standaard: eerste aan
        self.cam_vars = {}
        for cam in cams:
            var = tk.BooleanVar(value=cam["id"] in selected)
            self.cam_vars[cam["id"]] = var
            ttk.Checkbutton(listbox, text=cam["name"], variable=var).pack(anchor="w", pady=2)

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(14, 0))
        self.view_btn = ttk.Button(btns, text="▶  Bekijken", command=self.start_streams)
        self.view_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="■  Stoppen", command=self.stop_streams,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        bottom = ttk.Frame(f)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Button(bottom, text="Vernieuw camera's", command=self.refresh_cameras).pack(side="left")
        ttk.Button(bottom, text="Uitloggen", command=self.logout).pack(side="right")

    def _selected_cameras(self):
        cams = self.cfg.get("cameras", [])
        chosen_ids = [cid for cid, v in self.cam_vars.items() if v.get()]
        return [c for c in cams if c["id"] in chosen_ids]

    def refresh_cameras(self):
        res = self.ensure_session()
        if not res:
            return
        client, _, _ = res
        self.status("Camera's opnieuw zoeken…")
        try:
            cams = client.discover_cameras()
        except (TuyaAPIError, requests.RequestException) as e:
            messagebox.showerror(APP_NAME, f"Zoeken mislukt:\n{e}")
            return
        if cams:
            self.cfg["cameras"] = [
                {"id": cam_id(d), "name": cam_name(d), "product_id": d.get("productId", "")}
                for d in cams
            ]
            save_config(self.cfg)
        self.status(f"{len(self.cfg.get('cameras', []))} camera('s) gevonden")
        self.show_cameras()

    def logout(self):
        if not messagebox.askyesno(APP_NAME, "Uitloggen en opgeslagen gegevens wissen?"):
            return
        self.stop_streams()
        for k in ("session", "password_enc", "cameras", "selected", "nickname"):
            self.cfg.pop(k, None)
        save_config(self.cfg)
        self.show_login()

    # -- streams starten/stoppen ------------------------------------------
    def start_streams(self):
        chosen = self._selected_cameras()
        if not chosen:
            messagebox.showwarning(APP_NAME, "Vink minstens één camera aan.")
            return
        if not BRIDGE_EXE.exists():
            messagebox.showerror(APP_NAME, f"Bridge niet gevonden:\n{BRIDGE_EXE}")
            return
        vlc = find_vlc()
        if not vlc:
            messagebox.showerror(APP_NAME, "VLC niet gevonden.\n"
                                 "Installeer VLC via https://www.videolan.org")
            return

        # onthoud selectie
        self.cfg["selected"] = [c["id"] for c in chosen]
        save_config(self.cfg)

        res = self.ensure_session()
        if not res:
            self.status("Inloggen mislukt.")
            return
        _, ecode, partner = res
        sid = self.cfg["session"]["sid"]
        port = int(self.cfg.get("port", DEFAULT_PORT))

        # bridge-config schrijven (addon-modus: alle gekozen camera's, 1 server)
        bridge_cfg = {
            "signing_key": TUYA_SIGNING_KEY,
            "sid": sid,
            "ecode": ecode,
            "partner": partner,
            "app_key": TUYA_APP_KEY,
            "device_id": self.cfg["device_id"],
            "package_name": TUYA_PACKAGE_NAME,
            "bridge_port": port,
            "cameras": [
                {"camera_id": c["id"], "camera_name": c["name"],
                 "product_id": c.get("product_id", "")}
                for c in chosen
            ],
        }
        BRIDGE_CONFIG_PATH.write_text(json.dumps(bridge_cfg, indent=2), encoding="utf-8")

        self.view_btn.config(state="disabled")
        self.status("Bridge starten…")
        try:
            self.bridge_proc = subprocess.Popen(
                [str(BRIDGE_EXE), "addon", "--config", str(BRIDGE_CONFIG_PATH)],
                cwd=str(ROOT), creationflags=NO_WINDOW)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Bridge starten mislukt:\n{e}")
            self.view_btn.config(state="normal")
            return

        # wachten tot RTSP-poort open is
        deadline = time.time() + 25
        while time.time() < deadline:
            if self.bridge_proc.poll() is not None:
                messagebox.showerror(APP_NAME,
                                     "Bridge stopte voortijdig. Zijn de credentials verlopen?\n"
                                     "Probeer 'Uitloggen' en opnieuw inloggen.")
                self.view_btn.config(state="normal")
                return
            if port_open(port):
                break
            time.sleep(0.4)
            self.update()
        else:
            messagebox.showerror(APP_NAME, f"RTSP-poort {port} kwam niet online.")
            self.stop_streams()
            self.view_btn.config(state="normal")
            return

        # echte RTSP-paden ophalen (addon schrijft ze naar .tuya-data/cameras.json)
        paths = self._read_rtsp_paths()

        self.status("VLC-vensters openen…")
        self.vlc_procs = []
        for i, c in enumerate(chosen):
            path = paths.get(c["id"]) or ("/" + c["name"].replace(" ", "_"))
            url = f"rtsp://127.0.0.1:{port}{path}"
            args = [vlc, url,
                    "--no-one-instance",
                    "--video-on-top",
                    "--qt-minimal-view",
                    "--no-video-title-show",
                    "--no-qt-privacy-ask",
                    "--no-qt-error-dialogs",
                    "--rtsp-tcp",
                    "--network-caching=500",
                    f"--video-x={80 + i * 60}",
                    f"--video-y={80 + i * 60}",
                    f"--meta-title={c['name']}"]
            try:
                self.vlc_procs.append(subprocess.Popen(args))
            except OSError as e:
                messagebox.showwarning(APP_NAME, f"VLC openen mislukt voor {c['name']}:\n{e}")

        self.stop_btn.config(state="normal")
        self.status(f"Bezig: {len(self.vlc_procs)} camera('s) live. Sluit VLC of klik Stoppen.")

    def _read_rtsp_paths(self):
        """Map camera-id -> rtspPath uit .tuya-data/cameras.json."""
        out = {}
        try:
            data = json.loads((TUYA_DATA / "cameras.json").read_text(encoding="utf-8"))
            for c in data.get("cameras", []):
                if c.get("deviceId") and c.get("rtspPath"):
                    out[c["deviceId"]] = c["rtspPath"]
        except Exception:
            pass
        return out

    def stop_streams(self):
        for p in self.vlc_procs:
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass
        self.vlc_procs = []
        if self.bridge_proc is not None:
            try:
                if self.bridge_proc.poll() is None:
                    self.bridge_proc.terminate()
            except Exception:
                pass
            self.bridge_proc = None
        try:
            self.stop_btn.config(state="disabled")
            self.view_btn.config(state="normal")
        except (tk.TclError, AttributeError):
            pass
        self.status("Gestopt.")

    def on_close(self):
        self.stop_streams()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
