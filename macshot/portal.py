"""XDG desktop portal access.

On GNOME/Wayland the shell's own D-Bus screenshot API
(``org.gnome.Shell.Screenshot``) is guarded by an allow-list that contains only
``org.gnome.SettingsDaemon.MediaKeys`` and
``org.freedesktop.impl.portal.desktop.gnome`` (see ``DBusSenderChecker`` in
gnome-shell's ``js/misc/util.js``).  A third party application therefore has
exactly one sanctioned way to grab pixels: ``org.freedesktop.portal.Desktop``.

The portal answers a screenshot request in two steps: the method call returns a
request handle, and the actual result is delivered later as a ``Response``
signal on that handle.  The signal is only dispatched while a main loop is
running, so every call here drives a nested ``GLib.MainLoop``.
"""

from __future__ import annotations

import itertools
import os
from dataclasses import dataclass, field
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"

REQUEST_IFACE = "org.freedesktop.portal.Request"
SCREENSHOT_IFACE = "org.freedesktop.portal.Screenshot"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"

# org.freedesktop.portal.ScreenCast source types
SOURCE_MONITOR = 1
SOURCE_WINDOW = 2
# org.freedesktop.portal.ScreenCast cursor modes
CURSOR_HIDDEN = 1

_token_seq = itertools.count()
_request_seq = itertools.count()


class PortalError(RuntimeError):
    """Raised when the portal is missing, refuses a request or times out."""


class PortalCancelled(PortalError):
    """The user dismissed the portal's own dialog (not an error)."""


@dataclass
class RequestResult:
    """Outcome of one portal request."""

    code: int
    results: dict[str, Any] = field(default_factory=dict)

    @property
    def cancelled(self) -> bool:
        return self.code == 1

    @property
    def failed(self) -> bool:
        return self.code == 2


class Portal:
    """Thin client for the handful of portal calls macshot needs."""

    def __init__(self, bus: Gio.DBusConnection | None = None) -> None:
        self._bus = bus if bus is not None else Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if self._bus is None:
            raise PortalError("cannot connect to the session bus")

    @property
    def bus(self) -> Gio.DBusConnection:
        return self._bus

    # ------------------------------------------------------------------ core

    def _new_token(self, prefix: str) -> str:
        return f"macshot_{prefix}_{os.getpid()}_{next(_token_seq)}"

    def request(
        self,
        interface: str,
        method: str,
        build_parameters,
        *,
        options: dict[str, GLib.Variant] | None = None,
        timeout: float = 120.0,
        progress=None,
    ) -> RequestResult:
        """Call a portal method and wait for its ``Response`` signal.

        ``build_parameters`` receives the option dictionary (a unique
        ``handle_token`` plus whatever the caller passed in ``options``) and
        returns the full argument tuple for the method.  Keeping it a callable
        makes the trailing ``a{sv}`` explicit, which the portal requires: a
        missing ``interactive`` flag, for instance, makes GNOME pop up its own
        screenshot UI instead of grabbing the screen silently.
        """
        token = self._new_token(method.lower())
        opts = dict(options or {})
        opts["handle_token"] = GLib.Variant("s", token)
        parameters = build_parameters(opts)

        state: dict[str, Any] = {"response": None, "error": None, "handle": None}
        loop = GLib.MainLoop()

        def on_response(_conn, _sender, path, _iface, _signal, params):
            if token not in path:
                return
            code, results = params.unpack()
            state["response"] = RequestResult(code, results)
            loop.quit()

        def on_reply(conn, res, _user_data):
            try:
                state["handle"] = conn.call_finish(res).unpack()[0]
            except GLib.Error as exc:  # portal not installed, method missing, ...
                state["error"] = PortalError(f"{interface}.{method} failed: {exc.message}")
                loop.quit()

        def on_timeout():
            state["error"] = PortalError(f"{interface}.{method} timed out after {timeout:g}s")
            loop.quit()
            return GLib.SOURCE_REMOVE

        subscription = self._bus.signal_subscribe(
            # Neither the sender nor the path can be filtered here: the portal
            # may be service-activated by our own call (so a well-known-name
            # sender match would be registered before the name exists), and the
            # response is emitted on .../request/<sender>/<token> while gdbus
            # matches object paths exactly.  Requests are told apart by their
            # unique handle token instead.
            None, REQUEST_IFACE, "Response", None, None,
            Gio.DBusSignalFlags.NONE, on_response,
        )
        try:
            self._bus.call(
                PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, interface, method, parameters,
                None, Gio.DBusCallFlags.NONE, -1, None, on_reply, None,
            )
            if progress is not None:
                progress()
            timeout_id = GLib.timeout_add(int(timeout * 1000), on_timeout)
            loop.run()
            GLib.source_remove(timeout_id)
        finally:
            self._bus.signal_unsubscribe(subscription)

        if state["error"] is not None:
            raise state["error"]
        response = state["response"]
        if response is None:  # pragma: no cover - defensive
            raise PortalError(f"{interface}.{method} produced no response")
        if response.failed:
            raise PortalError(f"{interface}.{method} failed: {response.results!r}")
        return response

    # ------------------------------------------------------------- screenshot

    def screenshot(self, *, interactive: bool = False, timeout: float = 120.0,
                   progress=None) -> str:
        """Take a screenshot and return the path of the produced PNG file.

        With ``interactive=False`` GNOME grabs the whole desktop without showing
        any UI and hands back a file in the pictures folder; with
        ``interactive=True`` the desktop's own selection UI is shown and the
        result is whatever the user picked (cancellation raises ``PortalError``).
        """
        response = self.request(
            SCREENSHOT_IFACE, "Screenshot",
            lambda opts: GLib.Variant("(sa{sv})", ("", opts)),
            options={"interactive": GLib.Variant("b", interactive)},
            timeout=timeout, progress=progress,
        )
        if response.cancelled:
            raise PortalCancelled("screenshot request was cancelled")
        uri = response.results.get("uri")
        if not uri:
            raise PortalError(f"screenshot response carried no uri: {response.results!r}")
        path = Gio.File.new_for_uri(uri).get_path()
        if path is None:
            raise PortalError(f"portal returned a non-local uri: {uri}")
        return path

    # -------------------------------------------------------------- screencast

    def open_pipewire_remote_fd(self, session_handle: str) -> int:
        """Borrow the PipeWire file descriptor belonging to a screencast session."""
        variant, fd_list = self._bus.call_with_unix_fd_list_sync(
            PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, SCREENCAST_IFACE,
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (session_handle, {})),
            GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None,
        )
        if fd_list is None or fd_list.get_length() < 1:
            raise PortalError("portal did not hand back a PipeWire fd")
        index = variant.unpack()[0]
        return fd_list.get(index)
