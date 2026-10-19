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
from cancellation import CycleCancellation
from capture import SethCapture
from clicker import SethClicker
from hotkey import SethHotkey
from settings import load_config
from service_runtime import notify
from scroll_capture import ScrolledQuestion, ScrollLimitError, ScrollNavigationError, ScrollLayoutError
from trigger import SethTrigger, TriggerError, send_trigger
from vbox_backend import VMError, VirtualBoxBackend

LOG = logging.getLogger("seth")


class SethEngine:
    PROVIDERS = ("api", "gemini", "qwen")
    PROVIDER_LABELS = {"api": "API", "gemini": "Gemini", "qwen": "Qwen"}

    def __init__(self, config=None, execute=False, *, manual_selection=False, initial_provider="api"):
        self.config = config if config is not None else load_config()
        threshold = self.config.get("min_confidence", 0.85)
        if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("min_confidence must be a finite number from 0 to 1.")
        self.execute = execute
        self.backend = VirtualBoxBackend(self.config)
        self.capture = SethCapture(self.config, self.backend)
        self.stop_event = threading.Event()
        self.cancel_event = CycleCancellation(self.stop_event)
        self._state_lock = threading.Lock()
        self._cycle_active = False
        self.manual_selection = manual_selection
        if manual_selection and initial_provider not in self.PROVIDERS:
            raise ValueError("Manual inference provider must be api, gemini, or qwen.")
        self.selected_provider = initial_provider if manual_selection else None
        self._scroll_enabled = False if manual_selection else None
        self.ai = self._create_selected_client()
        self.clicker = SethClicker(self.config, self.backend)
        self._cycle_lock = threading.Lock()
        self._worker = None
        self.cycle_count = 0
        self.progress = ("idle", "Idle")

    def _create_selected_client(self):
        config = self.config
        if self.manual_selection:
            provider = self.selected_provider
            config = config | {"inference": {"backend": "api" if provider == "api" else "web"}}
            if provider != "api":
                config = config | {"web": config.get("web", {}) | {"providers": [provider]}}
        return create_client(config, stop_event=self.cancel_event,
                             progress=lambda label: setattr(self, "progress", ("thinking", label)))

    @property
    def scroll_enabled(self):
        if self._scroll_enabled is not None:
            return self._scroll_enabled
        return self.config.get("capture", {}).get("scroll_context", {}).get("enabled", False)

    @property
    def indicator_selection(self):
        if not self.manual_selection:
            return None
        return self.PROVIDER_LABELS[self.selected_provider] + " · " + ("Scroll" if self.scroll_enabled else "Single")

    def cycle_provider(self):
        if not self.manual_selection or self.stop_event.is_set() or not self._cycle_lock.acquire(blocking=False):
            return False
        try:
            previous = self.selected_provider
            self.selected_provider = self.PROVIDERS[(self.PROVIDERS.index(previous) + 1) % len(self.PROVIDERS)]
            try:
                self.ai = self._create_selected_client()
            except Exception:
                self.selected_provider = previous
                raise
            LOG.info("Selected inference: %s", self.indicator_selection)
            return True
        finally:
            self._cycle_lock.release()

    def toggle_capture(self):
        if not self.manual_selection or self.stop_event.is_set() or not self._cycle_lock.acquire(blocking=False):
            return False
        try:
            self._scroll_enabled = not self.scroll_enabled
            LOG.info("Selected capture: %s", self.indicator_selection)
            return True
        finally:
            self._cycle_lock.release()

    def run_cycle(self):
        if not self._cycle_lock.acquire(blocking=False):
            return {"status": "busy"}
        self._begin_cycle()
        return self._run_locked_cycle()

    def request_cycle(self):
        if self.stop_event.is_set() or not self._cycle_lock.acquire(blocking=False):
            return False
        self._begin_cycle()
        self._worker = threading.Thread(target=self._run_locked_cycle, name="seth-cycle")
        try:
            self._worker.start()
        except Exception:
            with self._state_lock:
                self._cycle_active = False
            self._cycle_lock.release()
            raise
        return True

    def _begin_cycle(self):
        with self._state_lock:
            self.cancel_event.clear()
            self._cycle_active = True

    def interrupt_cycle(self):
        with self._state_lock:
            if not self._cycle_active:
                self.progress = ("idle", "Idle")
                return False
            self.cancel_event.set()
            self.progress = ("cancelled", "Cancelling")
            return True

    def _check_cancelled(self):
        if self.cancel_event.is_set():
            raise AIError("Cycle cancelled; no further selections.", "cancelled")

    def _run_locked_cycle(self):
        try:
            return self._execute_cycle()
        finally:
            # Keep provider/mode stable through result metadata and logging too.
            with self._state_lock:
                self._cycle_active = False
                if self.cancel_event.interrupted.is_set():
                    self.progress = ("idle", "Idle")
                self._cycle_lock.release()

    def _execute_cycle(self):
        result = {"status": "error"}
        completed_clicks = []
        question = None
        navigation = None
        decision = None
        try:
            if self.cancel_event.is_set():
                result = {"status": "cancelled"}
                return result
            self.progress = ("capturing", "Capturing")
            self.ai.validate_config()
            frame = self.capture.capture_frame()
            self._check_cancelled()
            LOG.info("Captured %s (VM %s, screen %s, %sx%s)", frame.path,
                     frame.display.vm_uuid, frame.display.screen, frame.display.width, frame.display.height)
            if (getattr(self.ai, "supports_scroll_context", False)
                    and self.scroll_enabled):
                from scroll_capture import ScrollCollector
                capture_options = self.config.get("capture", {})
                scroll_config = self.config | {"capture": capture_options | {
                    "scroll_context": capture_options.get("scroll_context", {}) | {"enabled": True}}}
                question = ScrollCollector(scroll_config, self.capture, self.cancel_event,
                                           report=lambda label: setattr(self, "progress", ("capturing", label))).collect(frame, layout_client=self.ai)
                frame = question.frame
                if isinstance(question, ScrolledQuestion) and question.layout:
                    LOG.info('Discovered scroll layout: %s', json.dumps(question.layout))
                if question.contexts:
                    LOG.info("Collected %s scroll context sections; live answer frame %s", len(question.contexts), frame.path)
                if isinstance(question, ScrolledQuestion) and (question.views or question.layout):
                    navigation = question.navigate(self.config, self.capture, self.cancel_event,
                                                   report=lambda label: setattr(self, 'progress', ('capturing', label)))
                    LOG.info('Indexed %s distinct answer controls across %s views', len(navigation.controls), len(question.views))
            self._check_cancelled()
            LOG.info("Waiting for Seth to identify the answer selections...")
            self.progress = ("thinking", "Waiting")
            if navigation is not None:
                raw = self.ai.analyze_screenshot(frame.path, context_images=navigation.contexts,
                                                 control_regions=question.regions, scroll_targets=navigation.payload)
                self._check_cancelled()
                decision = self.ai.parse_coordinates(raw, frame.display.width, frame.display.height, scroll_navigation=True)
            else:
                raw = (self.ai.analyze_screenshot(frame.path, context_images=question.contexts, control_regions=question.regions)
                       if question is not None and question.contexts else self.ai.analyze_screenshot(frame.path))
                self._check_cancelled()
                decision = self.ai.parse_coordinates(raw, frame.display.width, frame.display.height)
            plan = navigation.plan(decision) if navigation is not None and decision is not None else None
            if self.cancel_event.is_set():
                result = {"status": "cancelled"}
            elif decision is None:
                result = {"status": "no_action"}
                explanation = getattr(self.ai, "last_no_action_explanation", None)
                if explanation:
                    result["explanation"] = explanation
                confidence = getattr(self.ai, "last_no_action_confidence", None)
                if confidence is not None:
                    result["confidence"] = confidence
            elif decision["confidence"] < self.config.get("min_confidence", 0.85):
                result = {"status": "low_confidence", "decision": decision}
            elif not self.execute:
                result = {"status": "dry_run", "decision": decision}
            else:
                self.progress = ("selecting", "Selecting")
                grounded = getattr(self.ai, "last_grounded", False) or getattr(self.ai, "last_provider", None) == "qwen"
                if navigation is not None:
                    for selection in plan:
                        target_frame, target = navigation.prepare(selection)
                        self.progress = ('selecting', f'Selecting {len(completed_clicks) + 1}/{len(plan)}')
                        self.capture.verify_target(target_frame, target['click_x'], target['click_y'])
                        click = self.clicker.click(target_frame, target['click_x'], target['click_y'], self.cancel_event)
                        completed_clicks.append(click | {key: target[key] for key in
                                                        ('target_id', 'target_view', 'row', 'choice', 'option') if key in target})
                        navigation.verify_selection(target_frame, target)
                        completed_clicks[-1]['selection_verified'] = True
                        navigation.confirm(target)
                        self.progress = ('selecting', f'Selected {len(completed_clicks)}/{len(plan)}')
                    result = {'status': 'clicked', 'decision': decision, 'click_count': len(completed_clicks), 'clicks': completed_clicks}
                elif decision["action"] == "select":
                    for selection in decision["selections"]:
                        if self.cancel_event.is_set():
                            raise VMError("Cycle cancelled; no further selections.")
                        if question is not None and question.contexts:
                            question.verify_identity(self.capture, self.cancel_event)
                        self.capture.verify_target(frame, selection["click_x"], selection["click_y"])
                        click = self.clicker.click(frame, selection["click_x"], selection["click_y"], self.cancel_event)
                        completed_clicks.append(click | {key: selection[key] for key in ("row", "choice", "option") if key in selection})
                        if grounded:
                            self.capture.verify_selection(frame, selection, self.cancel_event)
                            completed_clicks[-1]["selection_verified"] = True
                            if question is not None and question.contexts:
                                question.selected_controls.append(selection)
                        self.progress = ("selecting", f"Selected {len(completed_clicks)}/{len(decision['selections'])}")
                    result = {"status": "clicked", "decision": decision, "click_count": len(completed_clicks), "clicks": completed_clicks}
                else:
                    if question is not None and question.contexts:
                        question.verify_identity(self.capture, self.cancel_event)
                    if grounded:
                        self.capture.verify_target(frame, decision["click_x"], decision["click_y"])
                    click = self.clicker.click(frame, decision["click_x"], decision["click_y"], self.cancel_event)
                    if grounded:
                        completed_clicks.append(click)
                        self.capture.verify_selection(frame, decision, self.cancel_event)
                        click["selection_verified"] = True
                    result = {"status": "clicked", "decision": decision, "click": click}
                self.cycle_count += 1
        except (AIError, VMError, OSError, ValueError, TypeError) as error:
            result = {"status": "error", "error": str(error)}
            if isinstance(error, AIError):
                result["error_code"] = error.code
                if error.code == "cancelled":
                    result["status"] = "cancelled"
            elif isinstance(error, ScrollLimitError):
                result.update(error_code="scroll_limit", required_views=error.required_views, max_views=error.max_views)
            elif isinstance(error, ScrollNavigationError):
                result['error_code'] = 'scroll_navigation'
            elif isinstance(error, ScrollLayoutError):
                result['error_code'] = 'scroll_layout'
        except Exception:
            # Do not dump request objects, headers, or credentials in a traceback.
            result = {"status": "error", "error": "Unexpected cycle failure; no further action will be attempted."}
        finally:
            if self.cancel_event.is_set():
                result.update(status="cancelled", error_code="cancelled")
            status = result["status"]
            label = {"clicked": "Done", "dry_run": "Dry run ready", "no_action": "No selection",
                     "low_confidence": "Uncertain", "cancelled": "Cancelled", "error": "Error"}.get(status, status)
            if result.get("error_code") in ("response_timeout", "connect_timeout"):
                label = "API timeout"
            if result.get("error_code") == "gateway_unavailable":
                label = "API offline"
            if result.get("error_code") == "scroll_limit":
                label = "Scroll limit"
            if result.get('error_code') == 'scroll_navigation':
                label = 'Scroll stopped'
            if result.get('error_code') == 'scroll_layout':
                label = 'Scroll layout'
            if result.get("error_code", "").startswith("browser_"):
                label = {"browser_timeout": "Web timeout", "browser_login": "Web sign-in",
                         "browser_limit": "Web limit", "browser_busy": "Browser busy",
                         "browser_model": "Web model", "browser_layout": "Web layout"}.get(
                             result["error_code"], "Web error")
            if completed_clicks and status != "clicked":
                label = f"Stopped ({len(completed_clicks)})"
            self.progress = ("done" if status == "clicked" else status, label)
        if completed_clicks and result["status"] != "clicked":
            result["clicks_completed"] = len(completed_clicks)
            result["clicks"] = completed_clicks
            if decision is not None:
                result["decision"] = decision
        if getattr(self.ai, "last_provider", None):
            result["inference_provider"] = self.ai.last_provider
        if getattr(self.ai, "last_attempts", None):
            result["inference_attempts"] = self.ai.last_attempts
        if self.manual_selection:
            result["inference_provider"] = self.selected_provider
            result["capture_mode"] = "scroll" if self.scroll_enabled else "single"
        if question is not None and question.contexts:
            result["context_sections"] = len(question.contexts)
            result["capture_path"] = str(question.frame.path)
        if navigation is not None:
            result['scroll_navigation'] = True
            result['returned_views'] = navigation.returned_views
        if isinstance(question, ScrolledQuestion) and question.layout:
            result['scroll_layout'] = question.layout
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
    parser.add_argument("--hotkey", action="store_true", help="Enable run, provider, and capture shortcuts in listen mode (starts API / Single)")
    parser.add_argument("--overlay", action="store_true", help="Show a host indicator; Ctrl+Shift+D+F cycles modes, Ctrl+Shift+Q+W toggles text color (requires --hotkey)")
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
        initial_provider = "api"
        if args.hotkey and args.backend:
            initial_provider = ("api" if args.backend == "api" else "gemini" if args.backend == "gemini_web"
                                else config.get("web", {}).get("providers", ["gemini"])[0])
        engine = SethEngine(config, execute=args.execute, manual_selection=args.hotkey, initial_provider=initial_provider)

        def stop(signum, frame):
            engine.stop_event.set()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        if args.mode != "listen":
            result = engine.run_cycle()
            print(json.dumps(result))
            return 1 if result["status"] == "error" else 0
        # Missing credentials/session must not prevent switching to another
        # provider. Interactive selection validates only when a cycle is run.
        if not engine.manual_selection:
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
                                    toggle_callback=overlay.toggle if overlay is not None else None,
                                    provider_callback=engine.cycle_provider, capture_callback=engine.toggle_capture,
                                    text_color_callback=overlay.toggle_text_color if overlay is not None else None,
                                    interrupt_callback=engine.interrupt_cycle)
            if hotkey is not None:
                hotkey.start()
            LOG.info("Host listener ready (%s). Run: python3 main.py --mode trigger", "clicks enabled" if args.execute else "dry run")
            if hotkey is not None:
                LOG.info("Press Ctrl+D+F together, then release to capture and answer one question.")
                LOG.info("Ctrl+Shift+G interrupts the current run and returns to idle.")
                LOG.info("Ctrl+E+R cycles API / Gemini / Qwen; Ctrl+Shift+E+R toggles Single / Scroll. Selected: %s", engine.indicator_selection)
            if overlay is not None:
                overlay.poll(engine.progress, engine.indicator_selection)
                LOG.info("Host indicator ready (Full, white text). Ctrl+Shift+D+F cycles Hidden / Dot / Full; "
                         "Ctrl+Shift+Q+W toggles white / black text.")

            def poll():
                if hotkey is not None:
                    hotkey.poll()
                if overlay is not None:
                    overlay.poll(engine.progress, engine.indicator_selection)

            notify("READY=1\nSTATUS=Listener, shortcuts and indicator ready")
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
