#!/usr/bin/env python3
"""Seth: explicitly triggered capture -> inference -> one question's selections."""

import argparse
from dataclasses import asdict
import json
import logging
import math
from pathlib import Path
import signal
import threading
import time

from inference import AIError, create_client
from capture import SethCapture
from clicker import SethClicker
from hotkey import SethHotkey
from settings import load_config
from trigger import SethTrigger, TriggerError, send_trigger
from vbox_backend import VMError, VirtualBoxBackend

LOG = logging.getLogger("seth")


class SethEngine:
    def __init__(self, config=None, execute=False):
        self.config = config if config is not None else load_config()
        threshold = self.config.get("min_confidence", 0.85)
        if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("min_confidence must be a finite number from 0 to 1.")
        self.execute = execute
        self.backend = VirtualBoxBackend(self.config)
        self.capture = SethCapture(self.config, self.backend)
        self.stop_event = threading.Event()
        self.ai = create_client(self.config, stop_event=self.stop_event,
                                progress=lambda label: setattr(self, "progress", ("thinking", label)))
        self.clicker = SethClicker(self.config, self.backend)
        self._cycle_lock = threading.Lock()
        self._worker = None
        self.cycle_count = 0
        self.progress = ("idle", "Idle")

    def run_cycle(self):
        if not self._cycle_lock.acquire(blocking=False):
            return {"status": "busy"}
        return self._run_locked_cycle()

    def request_cycle(self):
        if self.stop_event.is_set() or not self._cycle_lock.acquire(blocking=False):
            return False
        self._worker = threading.Thread(target=self._run_locked_cycle, name="seth-cycle")
        try:
            self._worker.start()
        except Exception:
            self._cycle_lock.release()
            raise
        return True

    def _run_locked_cycle(self):
        result = {"status": "error"}
        completed_clicks = []
        try:
            if self.stop_event.is_set():
                result = {"status": "cancelled"}
                return result
            self.progress = ("capturing", "Capturing")
            self.ai.validate_config()
            frame = self.capture.capture_frame()
            LOG.info("Captured %s (VM %s, screen %s, %sx%s)", frame.path,
                     frame.display.vm_uuid, frame.display.screen, frame.display.width, frame.display.height)
            LOG.info("Waiting for Seth to identify the answer selections...")
            self.progress = ("thinking", "Waiting")
            raw = self.ai.analyze_screenshot(frame.path)
            decision = self.ai.parse_coordinates(raw, frame.display.width, frame.display.height)
            if self.stop_event.is_set():
                result = {"status": "cancelled"}
            elif decision is None:
                result = {"status": "no_action"}
            elif decision["confidence"] < self.config.get("min_confidence", 0.85):
                result = {"status": "low_confidence", "decision": decision}
            elif not self.execute:
                result = {"status": "dry_run", "decision": decision}
            else:
                self.progress = ("selecting", "Selecting")
                if decision["action"] == "select":
                    for selection in decision["selections"]:
                        if self.stop_event.is_set():
                            raise VMError("Cycle cancelled; no further selections.")
                        self.capture.verify_target(frame, selection["click_x"], selection["click_y"])
                        click = self.clicker.click(frame, selection["click_x"], selection["click_y"], self.stop_event)
                        completed_clicks.append(click | {key: selection[key] for key in ("row", "choice", "option") if key in selection})
                        self.progress = ("selecting", f"Selected {len(completed_clicks)}/{len(decision['selections'])}")
                    result = {"status": "clicked", "decision": decision, "click_count": len(completed_clicks), "clicks": completed_clicks}
                else:
                    click = self.clicker.click(frame, decision["click_x"], decision["click_y"], self.stop_event)
                    result = {"status": "clicked", "decision": decision, "click": click}
                self.cycle_count += 1
        except (AIError, VMError, OSError, ValueError, TypeError) as error:
            result = {"status": "error", "error": str(error)}
            if isinstance(error, AIError):
                result["error_code"] = error.code
                if error.code == "cancelled":
                    result["status"] = "cancelled"
        except Exception:
            # Do not dump request objects, headers, or credentials in a traceback.
            result = {"status": "error", "error": "Unexpected cycle failure; no further action will be attempted."}
        finally:
            status = result["status"]
            label = {"clicked": "Done", "dry_run": "Dry run ready", "no_action": "No selection",
                     "low_confidence": "Uncertain", "cancelled": "Cancelled", "error": "Error"}.get(status, status)
            if result.get("error_code") in ("response_timeout", "connect_timeout"):
                label = "API timeout"
            if result.get("error_code") == "gateway_unavailable":
                label = "API offline"
            if result.get("error_code", "").startswith("browser_"):
                label = {"browser_timeout": "Web timeout", "browser_login": "Web sign-in",
                         "browser_limit": "Web limit", "browser_busy": "Browser busy",
                         "browser_model": "Web model", "browser_layout": "Web layout"}.get(
                             result["error_code"], "Web error")
            if completed_clicks and status != "clicked":
                label = f"Stopped ({len(completed_clicks)})"
            self.progress = ("done" if status == "clicked" else status, label)
            self._cycle_lock.release()
        if completed_clicks and result["status"] != "clicked":
            result["clicks_completed"] = len(completed_clicks)
            result["clicks"] = completed_clicks
        if getattr(self.ai, "last_provider", None):
            result["inference_provider"] = self.ai.last_provider
        if getattr(self.ai, "last_attempts", None):
            result["inference_attempts"] = self.ai.last_attempts
        LOG.info("Cycle finished: %s", json.dumps(result))
        return result

    def wait(self):
        if self._worker is not None:
            self._worker.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Configuration JSON; .env is loaded from its directory")
    parser.add_argument("--backend", choices=("api", "web", "gemini_web"), help="Override inference backend for this process")
    parser.add_argument("--mode", choices=("once", "listen", "trigger", "manual", "test", "status", "capture"), default="once",
                        help="once/manual/test perform one cycle; listen/trigger use host requests; status/capture never call inference or click")
    parser.add_argument("--execute", action="store_true", help="Enable the current question's selections, at most 10 clicks (default: dry run)")
    parser.add_argument("--hotkey", action="store_true", help="Enable host Ctrl+P+O in listen mode (Linux keyboard permissions required)")
    parser.add_argument("--overlay", action="store_true", help="Show a host status indicator; Ctrl+Shift+P+O toggles visibility (requires --hotkey)")
    parser.add_argument("--delay", type=float, default=0,
                        help="Wait this many seconds before sending a host trigger")
    args = parser.parse_args()
    if args.hotkey and args.mode != "listen":
        parser.error("--hotkey applies only to --mode listen")
    if args.overlay and not args.hotkey:
        parser.error("--overlay requires --hotkey")
    if not math.isfinite(args.delay) or not 0 <= args.delay <= 60:
        parser.error("--delay must be between 0 and 60 seconds")
    if args.delay and args.mode != "trigger":
        parser.error("--delay applies only to --mode trigger")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    engine = None
    try:
        config = load_config(args.config)
        if args.backend:
            config.setdefault("inference", {})["backend"] = args.backend
        if args.mode == "status":
            print(json.dumps(VirtualBoxBackend(config).status()))
            return 0
        if args.mode == "capture":
            frame = SethCapture(config).capture_frame()
            print(json.dumps({"path": str(frame.path), "display": asdict(frame.display)}))
            return 0
        if args.mode == "trigger":
            if args.delay:
                LOG.info("Sending the host request in %.1f seconds.", args.delay)
                time.sleep(args.delay)
            result = send_trigger(config["project_dir"])
            print(result)
            return 0 if result == "accepted" else 2
        engine = SethEngine(config, execute=args.execute)

        def stop(signum, frame):
            engine.stop_event.set()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        if args.mode != "listen":
            result = engine.run_cycle()
            print(json.dumps(result))
            return 1 if result["status"] == "error" else 0
        engine.ai.validate_config()
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
                                    toggle_callback=overlay.toggle if overlay is not None else None)
            if hotkey is not None:
                hotkey.start()
            LOG.info("Host listener ready (%s). Run: python3 main.py --mode trigger", "clicks enabled" if args.execute else "dry run")
            if hotkey is not None:
                LOG.info("Press Ctrl+P+O together, then release to capture and answer one question.")
            if overlay is not None:
                LOG.info("Host indicator ready. Ctrl+Shift+P+O shows/hides it without starting a cycle.")

            def poll():
                if hotkey is not None:
                    hotkey.poll()
                if overlay is not None:
                    overlay.poll(engine.progress)

            trigger.serve(engine.stop_event, poll=poll if hotkey is not None else None)
        finally:
            engine.stop_event.set()
            if hotkey is not None:
                hotkey.stop()
            if overlay is not None:
                overlay.stop()
            trigger.stop()
            engine.wait()
        return 0
    except KeyboardInterrupt:
        return 130
    except (AIError, VMError, TriggerError, OSError, ValueError, TypeError) as error:
        LOG.error("%s", error)
        return 1
    finally:
        if engine is not None:
            engine.stop_event.set()


if __name__ == "__main__":
    raise SystemExit(main())
