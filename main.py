#!/usr/bin/env python3
"""Seth: explicitly triggered capture -> inference -> at most one click."""

import argparse
from dataclasses import asdict
import json
import logging
import math
from pathlib import Path
import signal
import threading
import time

from ai_client import AIError, SethAIClient
from capture import SethCapture
from clicker import SethClicker
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
        self.ai = SethAIClient(self.config)
        self.clicker = SethClicker(self.config, self.backend)
        self.stop_event = threading.Event()
        self._cycle_lock = threading.Lock()
        self._worker = None
        self.cycle_count = 0

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
        try:
            if self.stop_event.is_set():
                return {"status": "cancelled"}
            self.ai.validate_config()
            frame = self.capture.capture_frame()
            LOG.info("Captured %s (VM %s, screen %s, %sx%s)", frame.path,
                     frame.display.vm_uuid, frame.display.screen, frame.display.width, frame.display.height)
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
                click = self.clicker.click(frame, decision["click_x"], decision["click_y"], self.stop_event)
                self.cycle_count += 1
                result = {"status": "clicked", "decision": decision, "click": click}
        except (AIError, VMError, OSError, ValueError, TypeError) as error:
            result = {"status": "error", "error": str(error)}
        except Exception:
            # Do not dump request objects, headers, or credentials in a traceback.
            result = {"status": "error", "error": "Unexpected cycle failure; no further action will be attempted."}
        finally:
            self._cycle_lock.release()
        LOG.info("Cycle finished: %s", json.dumps(result))
        return result

    def wait(self):
        if self._worker is not None:
            self._worker.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Configuration JSON; .env is loaded from its directory")
    parser.add_argument("--mode", choices=("once", "listen", "trigger", "manual", "test", "status", "capture"), default="once",
                        help="once/manual/test perform one cycle; listen/trigger use host requests; status/capture never call inference or click")
    parser.add_argument("--execute", action="store_true", help="Enable one real click per accepted cycle (default: dry run)")
    parser.add_argument("--delay", type=float, default=0,
                        help="Wait this many seconds before sending a host trigger")
    args = parser.parse_args()
    if not math.isfinite(args.delay) or not 0 <= args.delay <= 60:
        parser.error("--delay must be between 0 and 60 seconds")
    if args.delay and args.mode != "trigger":
        parser.error("--delay applies only to --mode trigger")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    engine = None
    try:
        config = load_config(args.config)
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
        try:
            trigger.start()
            LOG.info("Host listener ready (%s). Run: python3 main.py --mode trigger", "clicks enabled" if args.execute else "dry run")
            trigger.serve(engine.stop_event)
        finally:
            engine.stop_event.set()
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
