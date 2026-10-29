"""Изолированный Windows-прототип; вся предметная логика остаётся в Application."""
from pathlib import Path
from threading import Lock, Thread
import json
import os
import platform
import sys
import tempfile
import time

from food_processes import Application


class Bridge:
    def __init__(self, app=None):
        self._app = app or Application()
        self._lock = Lock()

    def request(self, request):
        with self._lock:
            try:
                return self._app.request(request)
            except Exception:
                return {"api_version": "0.1", "request_id": request.get("request_id") if isinstance(request, dict) else None, "outcome": "error", "data": None, "diagnostics": [{"code": "bridge.internal_error", "category": "bridge", "severity": "error", "message": "Не удалось обработать запрос. Проверьте состояние сеанса."}]}


class ProbeBridge(Bridge):
    """Только для инструментального запуска; обычное окно этот метод не предоставляет."""
    def report(self, result):
        message = json.dumps(result, ensure_ascii=False)
        target = os.environ.get("SPIKE_PROBE_FILE")
        if target:
            Path(target).write_text(message, encoding="utf-8")
        if sys.stdout is not None:
            print("PROBE " + message, flush=True)
        if os.environ.get("SPIKE_PROBE_AUTOCLOSE") == "1":
            def close_probe():
                import webview
                time.sleep(0.2)
                webview.windows[0].destroy()
            Thread(target=close_probe, daemon=True).start()
        return True


def require_webview2():
    """Проверка известных требований до загрузки WinForms, чтобы не переключиться на MSHTML."""
    if sys.platform != "win32":
        raise RuntimeError("Прототип предназначен для Windows с WebView2 Runtime.")
    import winreg
    net_path = r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, net_path) as key:
            release = winreg.QueryValueEx(key, "Release")[0]
    except OSError as exc:
        raise RuntimeError("Не найден .NET Framework 4.6.2 или новее.") from exc
    if release < 394802:
        raise RuntimeError("Требуется .NET Framework 4.6.2 или новее.")
    runtime_key = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        prefix = "SOFTWARE\\" if hive == winreg.HKEY_CURRENT_USER or platform.machine() == "x86" else "SOFTWARE\\WOW6432Node\\"
        try:
            with winreg.OpenKey(hive, prefix + runtime_key) as key:
                version = winreg.QueryValueEx(key, "pv")[0]
            if isinstance(version, str) and tuple(map(int, version.split("."))) >= (86, 0, 622, 0):
                return version
        except (OSError, ValueError, TypeError):
            continue
    raise RuntimeError("Не найден WebView2 Runtime. Установите Microsoft Edge WebView2 Runtime.")


def main():
    started = time.perf_counter()
    try:
        require_webview2()
    except RuntimeError as exc:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "Food Processes Interactive", 0x10)
        raise SystemExit(str(exc)) from exc
    import webview
    html = Path(__file__).resolve().with_name("spike.html").read_text(encoding="utf-8")
    probe = "--probe" in sys.argv
    probe_data_dir = os.environ.get("SPIKE_PROBE_DATA_DIR") if probe else None
    probe_dir = tempfile.TemporaryDirectory(prefix="food-processes-gui-probe-") if probe and not probe_data_dir else None
    if probe:
        script = Path(__file__).resolve().with_name("spike_probe.js").read_text(encoding="utf-8")
        html = html.replace("</body>", "<script>" + script + "</script></body>")
    bridge = ProbeBridge(Application(probe_data_dir or probe_dir.name)) if probe else Bridge()
    window = webview.create_window("Food Processes Interactive — технический прототип", html=html, js_api=bridge, width=1080, height=720, min_size=(700, 500))
    if probe:
        def report_loaded():
            from webview.guilib import guilib
            if sys.stdout is not None:
                print(f"PROBE loaded renderer={guilib.renderer} seconds={time.perf_counter() - started:.3f}", flush=True)
        window.events.loaded += report_loaded
    try:
        webview.start(gui="edgechromium", debug=False)
    finally:
        if probe_dir:
            probe_dir.cleanup()


if __name__ == "__main__":
    main()
