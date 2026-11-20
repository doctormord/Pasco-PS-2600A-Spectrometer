"""
gui_remote.py — "Remote access" sidebar section of the desktop app.

RemotePanel starts/stops the API server (server_runner.ServerRunner) on the
shared engine and shows who currently holds control. It does not subscribe to
the engine itself; the host window forwards control/job changes through
update_control() / update_job().
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

import gui_theme as theme
from api import ServerSettings, new_token
from app_config import Config
from engine import LOCAL
from gui_theme import ToggleSwitch, make_row

LAN_HINT = "Unencrypted HTTP — use a TLS reverse proxy outside the lab."
_MASK = "•" * 12


class _ElidedLabel(QLabel):
    """Single-line label that elides its text to the available width and
    never imposes its text width on the layout."""

    def __init__(self, text: str = "", object_name: str = "statusVal", parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self._full = ""
        self.setMinimumWidth(40)
        self.setMinimumHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.set_full_text(text)

    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str, tooltip: str | None = None) -> None:
        self._full = text
        self.setToolTip(text if tooltip is None else tooltip)
        self._elide()

    def _elide(self) -> None:
        fm = QFontMetrics(self.font())
        self.setText(fm.elidedText(self._full, Qt.TextElideMode.ElideMiddle,
                                   max(self.width() - 4, 10)))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._elide()


class RemotePanel(QWidget):
    server_state_changed = pyqtSignal(bool, str)     # running, url

    def __init__(self, engine, parent=None, *, runner_factory=None):
        super().__init__(parent)
        self.engine = engine
        self._factory = runner_factory or self._default_factory
        self._runner = None
        self._token_visible = False
        self._control: dict = {}
        self._job: dict = {}

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)

        self.switch = ToggleSwitch(False)
        self.switch.setToolTip("Start/stop the REST API and MCP endpoint")
        v.addWidget(make_row("Remote access", self.switch))

        self.spin_port = QSpinBox()
        self.spin_port.setRange(1024, 65535)
        self.spin_port.setFixedWidth(84)
        self.spin_port.setValue(int(Config.get("remote_port", 8000)))
        v.addWidget(make_row("Port", self.spin_port))

        self.combo_access = QComboBox()
        self.combo_access.addItem("Local only (127.0.0.1)", False)
        self.combo_access.addItem("LAN (all interfaces)", True)
        self.combo_access.setCurrentIndex(1 if Config.get("remote_lan", False) else 0)
        self.combo_access.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo_access.setMinimumContentsLength(14)
        self.combo_access.setFixedWidth(150)
        v.addWidget(make_row("Access", self.combo_access))

        # Fixed-height hint slot: text toggles, height does not.
        self.lbl_lan_hint = QLabel("")
        self.lbl_lan_hint.setWordWrap(True)
        self.lbl_lan_hint.setFixedHeight(48)
        self.lbl_lan_hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        v.addWidget(self.lbl_lan_hint)

        self.switch_token_local = ToggleSwitch(
            bool(Config.get("remote_require_token_local", False)))
        v.addWidget(make_row("Require token locally", self.switch_token_local))

        self.lbl_token = _ElidedLabel(_MASK)
        v.addWidget(self.lbl_token)
        self.btn_show = self._btn("Show", "ghostBtn")
        self.btn_copy_token = self._btn("Copy", "ghostBtn")
        self.btn_renew = self._btn("Renew", "ghostBtn")
        v.addLayout(self._btn_row(self.btn_show, self.btn_copy_token, self.btn_renew))

        self.lbl_url = _ElidedLabel("—")
        self.btn_copy_url = self._btn("Copy URL", "ghostBtn")
        self.btn_copy_url.setEnabled(False)
        v.addWidget(make_row("URL", self.btn_copy_url))
        v.addWidget(self.lbl_url)

        self.lbl_control = _ElidedLabel("Control: free")
        v.addWidget(self.lbl_control)
        self.lbl_job = _ElidedLabel("")
        v.addWidget(self.lbl_job)

        self.btn_cancel = self._btn("Cancel remote job", "dangerBtn")
        self.btn_revoke = self._btn("Revoke control", "dangerBtn")
        self.btn_cancel.setEnabled(False)
        self.btn_revoke.setEnabled(False)
        v.addLayout(self._btn_row(self.btn_cancel, self.btn_revoke))

        self.switch.toggled.connect(self._on_switch)
        self.spin_port.valueChanged.connect(
            lambda p: Config.set("remote_port", int(p)))
        self.combo_access.currentIndexChanged.connect(self._on_access_changed)
        self.switch_token_local.toggled.connect(
            lambda b: Config.set("remote_require_token_local", bool(b)))
        self.btn_show.clicked.connect(self._toggle_token_visible)
        self.btn_copy_token.clicked.connect(self._copy_token)
        self.btn_renew.clicked.connect(self.renew_token)
        self.btn_copy_url.clicked.connect(self._copy_url)
        self.btn_cancel.clicked.connect(self._cancel_job)
        self.btn_revoke.clicked.connect(self._revoke)

        self._refresh_token_label()
        self._apply_lan_state()
        self._apply_running_state()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _default_factory(engine, settings):
        from server_runner import ServerRunner
        return ServerRunner(engine, settings)

    @staticmethod
    def _btn(text: str, name: str) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName(name)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        return b

    @staticmethod
    def _btn_row(*buttons) -> QHBoxLayout:
        h = QHBoxLayout()
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(6)
        for b in buttons:
            h.addWidget(b, 1)
        return h

    @property
    def runner(self):
        return self._runner

    def _is_running(self) -> bool:
        return bool(self._runner is not None and self._runner.running)

    def _lan(self) -> bool:
        return bool(self.combo_access.currentData())

    def _token(self) -> str:
        tok = Config.get("remote_token", "") or ""
        if not tok:
            tok = new_token()
            Config.set("remote_token", tok)
            self._refresh_token_label()
        return tok

    def _set_switch(self, on: bool) -> None:
        self.switch.blockSignals(True)
        self.switch.setChecked(on)
        self.switch.blockSignals(False)
        self.switch.update()

    # ------------------------------------------------------------ server
    def autostart_if_enabled(self) -> bool:
        if Config.get("remote_enabled", False) and not self._is_running():
            return self.start_server()
        return self._is_running()

    def _on_switch(self, on: bool) -> None:
        if on:
            self.start_server()
        else:
            self.stop_server()

    def start_server(self) -> bool:
        if self._is_running():
            self._set_switch(True)
            return True
        lan = self._lan()
        settings = ServerSettings(
            host="0.0.0.0" if lan else "127.0.0.1",
            port=int(self.spin_port.value()),
            token=self._token(),
            require_token_local=bool(Config.get("remote_require_token_local", False)),
            auto_connect=False,
            owns_engine=False,
        )
        try:
            runner = self._factory(self.engine, settings)
            runner.start()
        except Exception as e:                                   # noqa: BLE001
            self._runner = None
            self._set_switch(False)
            self._apply_running_state()
            QMessageBox.warning(self, "Remote access",
                                f"Could not start the server:\n{e}")
            return False
        self._runner = runner
        Config.set("remote_enabled", True)
        self._set_switch(True)
        self._apply_running_state()
        self.server_state_changed.emit(True, self._url())
        return True

    def stop_server(self) -> None:
        self._stop(persist=True)

    def shutdown(self) -> None:
        """Stop for application exit; keeps remote_enabled for next start."""
        self._stop(persist=False)

    def _stop(self, persist: bool) -> None:
        r, self._runner = self._runner, None
        if r is not None:
            try:
                r.stop()
            except Exception:                                    # noqa: BLE001
                pass
        if persist:
            Config.set("remote_enabled", False)
            self._set_switch(False)
        self._apply_running_state()
        self.server_state_changed.emit(False, "")

    def _url(self) -> str:
        try:
            return str(self._runner.url) if self._runner is not None else ""
        except Exception:                                        # noqa: BLE001
            return ""

    # ------------------------------------------------------------ state
    def _apply_running_state(self) -> None:
        running = self._is_running()
        self.spin_port.setEnabled(not running)
        self.combo_access.setEnabled(not running)
        self.switch_token_local.setEnabled(not running and not self._lan())
        url = self._url() if running else ""
        if url:
            self.lbl_url.set_full_text(url, f"{url}\nAPI docs: {url}/docs")
        else:
            self.lbl_url.set_full_text("—", "Server stopped")
        self.btn_copy_url.setEnabled(bool(url))

    def _on_access_changed(self, _i: int) -> None:
        Config.set("remote_lan", self._lan())
        self._apply_lan_state()

    def _apply_lan_state(self) -> None:
        lan = self._lan()
        if lan:
            p = theme.Theme
            self.lbl_lan_hint.setStyleSheet(
                f"color:{p.WARN};background:{p.WARN_BG};"
                f"border:1px solid {p.WARN_BORDER};border-radius:4px;"
                f"padding:2px 6px;font-size:11px;")
            self.lbl_lan_hint.setText(LAN_HINT + " Token is always required.")
        else:
            self.lbl_lan_hint.setStyleSheet("")
            self.lbl_lan_hint.setText("")
        # Token is mandatory on LAN: show as forced on.
        self.switch_token_local.blockSignals(True)
        self.switch_token_local.setChecked(
            True if lan else bool(Config.get("remote_require_token_local", False)))
        self.switch_token_local.blockSignals(False)
        self.switch_token_local.update()
        self.switch_token_local.setEnabled(not self._is_running() and not lan)

    def restyle(self) -> None:
        """Call after a theme switch."""
        self._apply_lan_state()

    # ------------------------------------------------------------ token
    def _refresh_token_label(self) -> None:
        tok = Config.get("remote_token", "") or ""
        if not tok:
            self.lbl_token.set_full_text("(created on first start)", "")
            self.btn_show.setText("Show")
            return
        if self._token_visible:
            self.lbl_token.set_full_text(tok, "API token")
        else:
            self.lbl_token.set_full_text(_MASK, "API token (hidden)")
        self.btn_show.setText("Hide" if self._token_visible else "Show")

    def _toggle_token_visible(self) -> None:
        self._token_visible = not self._token_visible
        self._refresh_token_label()

    def _copy_token(self) -> None:
        QApplication.clipboard().setText(self._token())

    def _copy_url(self) -> None:
        url = self._url()
        if url:
            QApplication.clipboard().setText(url)

    def renew_token(self) -> None:
        ans = QMessageBox.question(
            self, "Renew token",
            "Generate a new token? Clients using the old token lose access.")
        if ans != QMessageBox.StandardButton.Yes:
            return
        was_running = self._is_running()
        Config.set("remote_token", new_token())
        self._refresh_token_label()
        if was_running:
            self._stop(persist=False)
            self.start_server()

    # ------------------------------------------------------------ engine info
    def update_control(self, info: dict) -> None:
        self._control = dict(info or {})
        owner = self._control.get("owner")
        reason = self._control.get("reason") or ""
        if owner:
            text = f"Control: {owner}" + (f" ({reason})" if reason else "")
        else:
            text = "Control: free"
        self.lbl_control.set_full_text(text)
        self._apply_buttons()

    def update_job(self, job: dict) -> None:
        self._job = dict(job or {})
        j = self._job
        if not j:
            self.lbl_job.set_full_text("")
            return
        pct = int(round(float(j.get("progress") or 0.0) * 100))
        text = f"{j.get('kind', '')} {j.get('status', '')} {pct} %"
        if j.get("status") == "waiting_for_user" and j.get("prompt"):
            text += f" — {j['prompt']}"
        self.lbl_job.set_full_text(text)

    def _apply_buttons(self) -> None:
        c = self._control
        owner = c.get("owner")
        lease = c.get("lease")
        self.btn_cancel.setEnabled(bool(c.get("job_id")) and bool(owner) and owner != LOCAL)
        self.btn_revoke.setEnabled(bool(lease) and lease != LOCAL)

    def _cancel_job(self) -> None:
        jid = self._control.get("job_id")
        if jid:
            try:
                self.engine.jobs.cancel(jid)
            except Exception as e:                               # noqa: BLE001
                QMessageBox.warning(self, "Remote access", str(e))

    def _revoke(self) -> None:
        self.update_control(self.engine.release_control(None, force=True))
