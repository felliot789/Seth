"""Single/scroll VM capture, one selected provider, and answer keys in the status bar."""

import argparse
from dataclasses import asdict
import json
import logging
import math
from pathlib import Path
import signal
import threading
import time

from cancellation import CycleCancellation
from capture import SethCapture
from hotkey import SethHotkey
from inference import AIError, PROVIDERS, QUESTION_TYPES, QUESTION_LABELS, create_client, parse_decision
from service_runtime import notify
from scroll_capture import ScrollCapture
from settings import load_config
from trigger import SethTrigger, TriggerError, send_trigger
from vbox_backend import VMError, VirtualBoxBackend

LOG = logging.getLogger("seth")


class SethEngine:
    def __init__(self, config=None, *, provider=None, question_type=None, capture_mode=None):
        self.config = config if config is not None else load_config()
        self.threshold = self.config.get("min_confidence", .85)
        if type(self.threshold) not in (int, float) or not math.isfinite(self.threshold) or not 0 <= self.threshold <= 1:
            raise ValueError("min_confidence must be a finite number from 0 to 1.")
        self.selected_provider = provider or self.config.get("provider", "api")
        self.question_type = question_type or self.config.get("question_type", "mc")
        self.capture_mode = capture_mode or self.config.get("capture", {}).get("mode", "single")
        if self.capture_mode not in ("single", "scroll"):
            raise ValueError("Capture mode must be single or scroll.")
        if self.selected_provider not in PROVIDERS or self.question_type not in QUESTION_TYPES:
            raise ValueError("Choose provider api/qwen/gemini and format mc/mcma/table.")
        self.backend = VirtualBoxBackend(self.config)
        self.capture = SethCapture(self.config, self.backend)
        self.stop_event = threading.Event()
        self.cancel_event = CycleCancellation(self.stop_event)
        self._cycle_lock, self._state_lock = threading.Lock(), threading.Lock()
        self._cycle_active, self._worker = False, None
        self.progress = ("idle", "Idle")

    @property
    def indicator_selection(self):
        return f"{self.selected_provider.upper() if self.selected_provider == 'api' else self.selected_provider.title()} · {QUESTION_LABELS[self.question_type]} · {self.capture_mode.title()}"

    def _cycle_choice(self, attribute, choices):
        if self.stop_event.is_set() or not self._cycle_lock.acquire(blocking=False):
            return False
        try:
            setattr(self, attribute, choices[(choices.index(getattr(self, attribute)) + 1) % len(choices)])
            self.progress = ("idle", "Ready")
            LOG.info("Selected %s", self.indicator_selection)
            return True
        finally:
            self._cycle_lock.release()

    def cycle_provider(self):
        return self._cycle_choice("selected_provider", PROVIDERS)

    def cycle_question_type(self):
        return self._cycle_choice("question_type", QUESTION_TYPES)

    def cycle_capture_mode(self):
        return self._cycle_choice("capture_mode", ("single", "scroll"))

    def _begin(self):
        if self.stop_event.is_set() or not self._cycle_lock.acquire(blocking=False):
            return False
        with self._state_lock:
            self.cancel_event.clear()
            self._cycle_active = True
        return True

    def run_cycle(self):
        return self._run_locked_cycle() if self._begin() else {"status": "busy"}

    def request_cycle(self):
        if not self._begin():
            return False
        self._worker = threading.Thread(target=self._run_locked_cycle, name="seth-cycle")
        try:
            self._worker.start()
        except Exception:
            self._cycle_active = False
            self._cycle_lock.release()
            raise
        return True

    def interrupt_cycle(self):
        with self._state_lock:
            if not self._cycle_active:
                return False
            self.cancel_event.set()
            self.progress = ("cancelled", "Cancelling")
            return True

    def _run_locked_cycle(self):
        try:
            return self._execute_cycle()
        finally:
            try:
                self.backend.close()
            finally:
                with self._state_lock:
                    self._cycle_active = False
                    if self.cancel_event.interrupted.is_set():
                        self.progress = ("idle", "Idle")
                    self._cycle_lock.release()

    def _execute_cycle(self):
        started, timings = time.perf_counter(), {}
        frame, decision, frames = None, None, ()
        try:
            client = create_client(self.config, self.selected_provider, stop_event=self.cancel_event,
                                   progress=lambda label: setattr(self, "progress", ("thinking", label)))
            client.validate_config()
            self.progress = ("capturing", "Capturing")
            if self.cancel_event.is_set():
                raise AIError("Capture cancelled.", "cancelled")
            if self.capture_mode == "scroll":
                frames = ScrollCapture(self.capture, self.config, self.cancel_event,
                                       lambda label: setattr(self, "progress", ("capturing", label))).run()
            else:
                frames = (self.capture.capture_frame(),)
            frame = frames[0]
            if self.cancel_event.is_set():
                raise AIError("Capture cancelled.", "cancelled")
            timings["capture_s"] = round(time.perf_counter() - started, 3)
            stage = time.perf_counter()
            self.progress = ("thinking", "Waiting")
            if self.capture_mode == "scroll":
                raw = client.analyze_screenshots([part.path for part in frames], self.question_type)
            else:
                raw = client.analyze_screenshot(frame.path, self.question_type)
            if self.cancel_event.is_set():
                raise AIError("Inference cancelled.", "cancelled")
            timings["inference_s"] = round(time.perf_counter() - stage, 3)
            reply_path = frame.path.with_suffix(".reply.txt")
            reply_path.write_text(raw)
            reply_path.chmod(0o600)
            decision = parse_decision(raw, self.question_type)
            if decision["status"] == "answer" and decision["confidence"] < self.threshold:
                decision.update(status="uncertain", answer="")
            result = {"status": decision["status"], "decision": decision}
        except (AIError, VMError, OSError, ValueError, TypeError) as error:
            result = {"status": "error", "error": str(error)}
            if isinstance(error, AIError):
                result["error_code"] = error.code
        except Exception:
            LOG.exception("Unexpected Seth capture/inference failure")
            result = {"status": "error", "error": "Unexpected failure; inspect seth logs."}
        if self.cancel_event.is_set():
            result.update(status="cancelled", error_code="cancelled")
        if frame is not None:
            result["capture_path"] = str(frame.path)
            result["capture_paths"] = [str(part.path) for part in frames]
        status = result["status"]
        label = (decision["answer"] if status == "answer" else
                 "Error: " + result["error"] if status == "error" else
                 {"uncertain": "Uncertain", "no_action": "No answer",
                  "cancelled": "Cancelled"}[status])
        self.progress = ("done" if status == "answer" else status, label)
        timings["total_s"] = round(time.perf_counter() - started, 3)
        result.update(provider=self.selected_provider, question_type=self.question_type,
                      capture_mode=self.capture_mode, timings=timings)
        LOG.info("Cycle finished: %s", json.dumps(result))
        return result

    def wait(self):
        if self._worker is not None:
            self._worker.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="JSON configuration; defaults to config.json beside main.py")
    parser.add_argument("--mode", choices=("once", "listen", "trigger", "status", "capture"), default="once",
                        help="Run once (default), serve/trigger the listener, inspect VM status, or save a capture")
    parser.add_argument("--provider", choices=PROVIDERS, help="Provider for once/listen; otherwise use config")
    parser.add_argument("--question-type", choices=QUESTION_TYPES, help="Manual question format for once/listen")
    parser.add_argument("--capture-mode", choices=("single", "scroll"), help="Capture one screen or scroll the whole page")
    parser.add_argument("--hotkey", action="store_true", help="Enable host shortcuts in listen mode")
    parser.add_argument("--overlay", action="store_true", help="Show the status bar with --hotkey")
    args = parser.parse_args()
    if args.hotkey and args.mode != "listen" or args.overlay and not args.hotkey:
        parser.error("--hotkey requires listen mode; --overlay requires --hotkey")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    engine = None
    backend = None
    try:
        config = load_config(args.config)
        if args.mode == "status":
            backend = VirtualBoxBackend(config)
            result = backend.status()
            print(json.dumps(result))
            return 0
        if args.mode == "trigger":
            result = send_trigger(config["project_dir"])
            print(result)
            return 0 if result == "accepted" else 2
        engine = SethEngine(config, provider=args.provider, question_type=args.question_type, capture_mode=args.capture_mode)
        signal.signal(signal.SIGINT, lambda *_: engine.stop_event.set())
        signal.signal(signal.SIGTERM, lambda *_: engine.stop_event.set())
        if args.mode == "capture":
            frames = (ScrollCapture(engine.capture, config, engine.cancel_event).run() if engine.capture_mode == "scroll"
                      else (engine.capture.capture_frame(),))
            print(json.dumps({"path": str(frames[0].path), "paths": [str(frame.path) for frame in frames],
                              "display": asdict(frames[0].display)}))
            return 0
        if args.mode == "once":
            result = engine.run_cycle()
            print(json.dumps(result))
            return 1 if result["status"] == "error" else 0
        trigger = SethTrigger(config["project_dir"], engine.request_cycle)
        overlay = None
        hotkey = None
        try:
            trigger.start()
            if args.overlay:
                from overlay import SethOverlay
                overlay = SethOverlay()
            if args.hotkey:
                hotkey = SethHotkey(engine.request_cycle, config.get("hotkey", {}).get("devices"),
                                    provider_callback=engine.cycle_provider,
                                    format_callback=engine.cycle_question_type,
                                    capture_mode_callback=engine.cycle_capture_mode,
                                    interrupt_callback=engine.interrupt_cycle,
                                    toggle_callback=overlay.toggle if overlay else None,
                                    text_color_callback=overlay.toggle_text_color if overlay else None)
                hotkey.start()
            LOG.info("Ready: Ctrl+D+F capture; Ctrl+E+R API/Qwen/Gemini; Ctrl+Shift+Z MC/MCMA/Table; Ctrl+Shift+S Single/Scroll; Ctrl+Shift+G cancel. No answer input.")
            def poll():
                if hotkey:
                    hotkey.poll()
                if overlay:
                    overlay.poll(engine.progress, engine.indicator_selection)
            notify("READY=1\nSTATUS=Seth ready")
            trigger.serve(engine.stop_event, poll=poll)
        finally:
            engine.stop_event.set()
            if hotkey:
                hotkey.stop()
            if overlay:
                overlay.stop()
            trigger.stop()
            engine.wait()
        return 0
    except (AIError, VMError, TriggerError, OSError, ValueError) as error:
        LOG.error("%s", error)
        return 1
    finally:
        if backend:
            backend.close()
        if engine:
            engine.stop_event.set()
            engine.backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
