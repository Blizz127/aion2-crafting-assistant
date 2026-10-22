#!/usr/bin/env python3
"""Bounded, recipe-neutral AION 2 crafting assistant for the Legion Go 2."""

from __future__ import annotations

import argparse
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "aion2-cleric-macro"))
import aion2_cleric_rotation as rotation  # noqa: E402


UINPUT_NAME = "aion2-crafting-assistant"
DEFAULT_INTERVAL = 12.0
DEFAULT_MAX_CLICKS = 10
CROP_WIDTH = 80
CROP_HEIGHT = 36


@dataclass(frozen=True)
class CraftConfig:
    interval: float = DEFAULT_INTERVAL
    max_clicks: int = DEFAULT_MAX_CLICKS

    def validate(self) -> None:
        if not 3.0 <= self.interval <= 300.0:
            raise ValueError("craft interval must be between 3 and 300 seconds")
        if not 1 <= self.max_clicks <= 999:
            raise ValueError("maximum crafts must be between 1 and 999")


@dataclass(frozen=True)
class ButtonReference:
    window_id: str
    cursor_x: int
    cursor_y: int
    left: int
    top: int
    image: bytes


def button_matches(reference: bytes, current: bytes, tolerance: float = 12.0) -> bool:
    """Return true only when the button-sized RGB samples still closely match."""
    if not reference or len(reference) != len(current):
        return False
    difference = sum(abs(first - second) for first, second in zip(reference, current))
    return difference / len(reference) <= tolerance


def parse_cursor_location(output: str) -> tuple[int, int]:
    values = dict(
        line.split("=", 1) for line in output.splitlines() if "=" in line
    )
    return int(values["X"]), int(values["Y"])


def parse_window_geometry(output: str) -> tuple[int, int, int, int]:
    def value(label: str) -> int:
        match = re.search(rf"^\s*{re.escape(label)}:\s*(-?\d+)", output, re.MULTILINE)
        if match is None:
            raise OSError(f"missing window geometry field: {label}")
        return int(match.group(1))

    return (
        value("Absolute upper-left X"),
        value("Absolute upper-left Y"),
        value("Width"),
        value("Height"),
    )


def find_aion_window() -> str:
    windows = subprocess.run(
        ["wmctrl", "-lx"], check=True, capture_output=True, text=True, timeout=1
    ).stdout
    window_id = rotation.find_aion_window_id(windows)
    if window_id is None:
        raise OSError("AION 2 window was not found")
    return window_id


def cursor_location() -> tuple[int, int]:
    output = subprocess.run(
        ["xdotool", "getmouselocation", "--shell"],
        check=True,
        capture_output=True,
        text=True,
        timeout=1,
    ).stdout
    return parse_cursor_location(output)


def capture_button_reference() -> ButtonReference:
    if not rotation.aion_process_running() or not rotation.aion_window_focused():
        raise OSError("AION 2 must be running and focused")
    window_id = find_aion_window()
    geometry = subprocess.run(
        ["xwininfo", "-id", window_id],
        check=True,
        capture_output=True,
        text=True,
        timeout=1,
    ).stdout
    root_x, root_y, width, height = parse_window_geometry(geometry)
    cursor_x, cursor_y = cursor_location()
    relative_x = cursor_x - root_x
    relative_y = cursor_y - root_y
    left = relative_x - CROP_WIDTH // 2
    top = relative_y - CROP_HEIGHT // 2
    if left < 0 or top < 0 or left + CROP_WIDTH > width or top + CROP_HEIGHT > height:
        raise OSError("pointer is too close to the edge of the AION 2 window")
    image = rotation.capture_window_crop(
        window_id, left, top, CROP_WIDTH, CROP_HEIGHT
    )
    return ButtonReference(window_id, cursor_x, cursor_y, left, top, image)


def reference_is_ready(reference: ButtonReference) -> bool:
    if not rotation.aion_window_focused():
        return False
    current_x, current_y = cursor_location()
    if abs(current_x - reference.cursor_x) > 2 or abs(current_y - reference.cursor_y) > 2:
        return False
    current = rotation.capture_window_crop(
        reference.window_id,
        reference.left,
        reference.top,
        CROP_WIDTH,
        CROP_HEIGHT,
    )
    return button_matches(reference.image, current)


class ClickInjector:
    def __init__(self) -> None:
        self.ui = rotation.UInput(
            {rotation.e.EV_KEY: [rotation.e.BTN_LEFT],
             rotation.e.EV_REL: [rotation.e.REL_X, rotation.e.REL_Y]}, name=UINPUT_NAME
        )

    def click(self) -> None:
        self.ui.write(rotation.e.EV_KEY, rotation.e.BTN_LEFT, 1)
        self.ui.syn()
        time.sleep(0.05)
        self.ui.write(rotation.e.EV_KEY, rotation.e.BTN_LEFT, 0)
        self.ui.syn()

    def close(self) -> None:
        if self.ui is not None:
            self.ui.close()
            self.ui = None


class CraftEngine:
    def __init__(
        self,
        config: CraftConfig,
        status: Callable[[str], None] = print,
    ) -> None:
        config.validate()
        self.config = config
        self.status = status
        self.commands: queue.SimpleQueue[tuple[str, object | None]] = queue.SimpleQueue()
        self.stopping = False

    def command(self, name: str, value: object | None = None) -> None:
        self.commands.put((name, value))

    def run(self) -> None:
        # Same toggle button as the combat helper (controls.json combat_toggle,
        # e.g. KEY_F12 on the Legion Go 2 where Y1 is presented as F12); F7 otherwise.
        toggle = rotation.resolve(rotation.load_flip_control() or "KEY_F7")
        start = rotation.e.KEY_F8
        panic = rotation.resolve(rotation.PANIC_KEY)
        selector = rotation.open_listeners({toggle, start, panic})
        injector = ClickInjector()
        debouncer = rotation.KeyDebouncer()
        active = False
        reference: ButtonReference | None = None
        clicks = 0
        next_due = 0.0

        def stop(reason: str) -> None:
            nonlocal active, reference
            active = False
            reference = None
            self.status(f"Stopped: {reason}")

        def toggle_active() -> None:
            nonlocal active, reference, clicks, next_due
            if active:
                stop("F7 toggle")
                return
            try:
                reference = capture_button_reference()
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                self.status(f"Not armed: {error}")
                return
            active = True
            clicks = 0
            next_due = time.monotonic()
            self.status(
                f"Crafting ON: up to {self.config.max_clicks} clicks, "
                f"{self.config.interval:g}s apart"
            )

        self.status(
            "Ready: select a recipe, hover over Craft/Repeat, then press F8; F9 stops"
        )
        try:
            while not self.stopping:
                while True:
                    try:
                        command, value = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    if command == "stop":
                        stop("window stop")
                    elif command == "quit":
                        self.stopping = True
                    elif command == "config" and isinstance(value, CraftConfig):
                        value.validate()
                        self.config = value
                        stop("settings updated")

                for key, _ in selector.select(timeout=0.05):
                    device = key.fileobj
                    try:
                        events = list(device.read())
                    except OSError:
                        selector.unregister(device)
                        device.close()
                        continue
                    for event in events:
                        if (
                            event.type != rotation.e.EV_KEY
                            or event.value != 1
                            or not debouncer.accept(event.code, time.monotonic())
                        ):
                            continue
                        if event.code == panic:
                            stop("F9 panic stop")
                        elif event.code == toggle:
                            toggle_active()
                        elif event.code == start and not active:
                            toggle_active()

                if self.stopping or not active or reference is None:
                    continue
                if not rotation.aion_process_running():
                    stop("AION 2 exited")
                    continue
                if not rotation.aion_window_focused():
                    stop("AION 2 lost focus")
                    continue
                now = time.monotonic()
                if now < next_due:
                    continue
                try:
                    ready = reference_is_ready(reference)
                except (OSError, subprocess.SubprocessError, ValueError):
                    ready = False
                if not ready:
                    stop("pointer moved or the Craft/Repeat button changed")
                    continue
                if not self.commands.empty() or selector.select(timeout=0):
                    continue
                if self.stopping or not rotation.aion_window_focused():
                    continue
                injector.click()
                clicks += 1
                self.status(f"Craft click {clicks}/{self.config.max_clicks}")
                if clicks >= self.config.max_clicks:
                    stop("craft limit reached")
                else:
                    next_due = now + self.config.interval
        finally:
            injector.close()
            for key in list(selector.get_map().values()):
                key.fileobj.close()
            selector.close()


def run_gui(config: CraftConfig, smoke_path: Path | None = None) -> None:
    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtWidgets import (
        QApplication,
        QDoubleSpinBox,
        QFormLayout,
        QLabel,
        QMainWindow,
        QPushButton,
        QSpinBox,
        QVBoxLayout,
        QWidget,
    )

    class Bridge(QObject):
        status = Signal(str)

    app = QApplication(sys.argv)
    bridge = Bridge()
    window = QMainWindow()
    window.setWindowTitle("AION 2 Crafting Assistant")
    body = QWidget()
    layout = QVBoxLayout(body)
    instructions = QLabel(
        "1. In AION 2, select any recipe and quantity.\n"
        "2. Leave the pointer over Craft or Repeat.\n"
        "3. Press F8 to start. F9 stops; F7 toggles.\n\n"
        "The assistant never selects recipes or buys materials."
    )
    instructions.setWordWrap(True)
    layout.addWidget(instructions)
    form = QFormLayout()
    interval = QDoubleSpinBox()
    interval.setRange(3.0, 300.0)
    interval.setValue(config.interval)
    interval.setSuffix(" seconds")
    maximum = QSpinBox()
    maximum.setRange(1, 999)
    maximum.setValue(config.max_clicks)
    form.addRow("Time between clicks", interval)
    form.addRow("Maximum craft clicks", maximum)
    layout.addLayout(form)
    status_label = QLabel("Starting…")
    status_label.setWordWrap(True)
    layout.addWidget(status_label)
    apply_button = QPushButton("Apply settings")
    stop_button = QPushButton("STOP NOW")
    layout.addWidget(apply_button)
    layout.addWidget(stop_button)
    window.setCentralWidget(body)
    window.resize(470, 310)

    engine = CraftEngine(config, bridge.status.emit)
    worker = threading.Thread(target=engine.run, daemon=True)
    bridge.status.connect(status_label.setText)
    apply_button.clicked.connect(
        lambda: engine.command(
            "config", CraftConfig(interval.value(), maximum.value())
        )
    )
    stop_button.clicked.connect(lambda: engine.command("stop"))
    if smoke_path is None:
        worker.start()
    window.show()

    if smoke_path is not None:
        QTimer.singleShot(250, lambda: (window.grab().save(str(smoke_path)), app.quit()))
    app.exec()
    engine.command("quit")
    if worker.is_alive():
        worker.join(timeout=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--max-clicks", type=int, default=DEFAULT_MAX_CLICKS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--gui-smoke-test", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = CraftConfig(args.interval, args.max_clicks)
    config.validate()
    if args.dry_run:
        print(
            f"AION 2 crafting assistant: F8 start, F7 toggle, F9 stop, "
            f"{config.max_clicks} clicks maximum, {config.interval:g}s interval"
        )
        print("No input devices were opened and no input was sent.")
        return
    lock = None
    if args.gui_smoke_test is None:
        try:
            lock = rotation.acquire_instance_lock()
        except SystemExit:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication(sys.argv)
            QMessageBox.information(None, "AION 2 helper already open",
                "Close the combat helper or other crafting window first, then open crafting again. "
                "They share F8/F9 and cannot run together.")
            return
    run_gui(config, args.gui_smoke_test)


if __name__ == "__main__":
    main()
