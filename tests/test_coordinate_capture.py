"""Unbadged inference and upload-to-original coordinate regressions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from ai_client import SethAIClient
from inference import AIError, DecisionClient
from control_targets import coordinate_capture, ground_capture_decision, catalog_controls
from tests.test_control_targets import draw_control


class CoordinateCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'frame.png'
        self.image = Image.new('RGB', (320, 500), 'white')
        for row in range(12):
            draw_control(self.image, 'checkbox', (60, 35 + row * 38))
        self.image.save(self.path)
        self.client = SethAIClient({'api': {'base_url': 'https://example.com/v1', 'api_key': 'test', 'model': 'test'}})

    def test_default_single_upload_is_exact_original_with_complete_mcma(self):
        response = {'action': 'select', 'selection_type': 'multiple_answer', 'coordinate_space': 'pixels',
                    'confidence': .99, 'explanation': 'Every option is correct.',
                    'selections': [{'option': str(i), 'click_x': 60, 'click_y': 35+i*38} for i in range(12)]}
        def exchange(upload, prompt, response_format):
            self.assertEqual(Path(upload), self.path)
            self.assertEqual(Path(upload).read_bytes(), self.path.read_bytes())
            self.assertNotIn('target_id', prompt)
            self.assertNotIn('badge', prompt)
            return json.dumps(response)
        with patch.object(self.client, '_analyze_screenshot', side_effect=exchange):
            result = json.loads(self.client.analyze_screenshot(self.path))
        self.assertEqual(len(result['selections']), 12)
        self.assertTrue(all('target_control' in p for p in result['selections']))

    def test_context_offsets_normalized_dimensions_and_reference_rejection(self):
        contexts = [{'path': self.path, 'box': [0, 0, 320, 100], 'label': 'Question'}]
        with coordinate_capture(self.path, context_images=contexts) as (upload, mappings):
            with Image.open(upload) as sheet:
                width, height = sheet.size
                self.assertEqual((width, height), (320, 684))
                self.assertEqual(sheet.getpixel((60, 35+184)), self.image.getpixel((60,35)))
            raw = {'action':'select', 'selection_type':'multiple_answer', 'coordinate_space':'normalized_1000',
                   'confidence':.99, 'explanation':'First', 'selections':[{'option':'A','click_x':188,'click_y':320}]}
            parsed = DecisionClient().parse_coordinates(json.dumps(raw), width, height)
            result = ground_capture_decision(self.path, parsed, mappings)
            self.assertEqual(result['selections'][0]['click_y'], 34)
            parsed['selections'][0].update(click_x=60, click_y=67)
            with self.assertRaises(AIError):
                ground_capture_decision(self.path, parsed, mappings)

    def test_long_document_maps_to_original_view_and_rejects_duplicate_copies(self):
        original = {'kind':'checkbox', 'box':[49,24,71,46], 'target_view':'first'}
        document = {'sections':[{'path':self.path, 'box':[20,10,300,490], 'offset':0},
                                {'path':self.path, 'box':[20,10,300,490], 'offset':200}],
                    'controls':{'01':{'kind':'checkbox','box':[29,14,51,36]}}}
        payload = {'controls':{'01':original}, 'live_controls':{'01':original}, 'document':document}
        with coordinate_capture(self.path, scroll_targets=payload) as (upload,mappings):
            with Image.open(upload) as image:
                self.assertEqual(image.size,(280,716))
            decision = {'action':'select','selection_type':'multiple_answer','selections':[
                {'option':'A','click_x':40,'click_y':61}], 'coordinate_space':'pixels','confidence':.99,'explanation':'A'}
            result = ground_capture_decision(self.path, decision, mappings)
            point = result['selections'][0]
            self.assertEqual((point['click_x'],point['click_y'],point['target_id'],point['target_view']), (60,34,'01','first'))
            decision['selections'].append({'option':'B','click_x':41,'click_y':62})
            with self.assertRaises(AIError): ground_capture_decision(self.path, decision,mappings)
            decision['selections'] = [{'option':'A','click_x':40,'click_y':8}]
            with self.assertRaises(AIError): ground_capture_decision(self.path,decision,mappings)

    def test_ambiguous_mapping_and_out_of_bounds_are_rejected(self):
        entry = {'upload_box':[10,10,30,30],'target_id':'01','control':{'kind':'radio','box':[10,10,30,30]}}
        decision = {'action':'click','click_x':20,'click_y':20}
        with self.assertRaises(AIError): ground_capture_decision(self.path,decision,[entry,entry])
        raw = dict(decision,coordinate_space='pixels',confidence=.99,explanation='A',click_y=500)
        with self.assertRaises(AIError): DecisionClient().parse_coordinates(json.dumps(raw),320,500)

    def test_multicontext_controls_map_to_canonical_views_and_duplicate_is_rejected(self):
        first = {'kind':'checkbox','box':[49,24,71,46], 'target_view':'view-a'}
        last = {'kind':'checkbox','box':[49,442,71,464], 'target_view':'view-b'}
        payload = {'controls':{'01':first,'12':last}, 'live_controls':{'12':last}}
        contexts = [{'path':self.path,'box':[0,0,160,100], 'controls':{'01':first}},
                    {'path':self.path,'box':[0,0,160,100], 'controls':{'01':first}}]
        with coordinate_capture(self.path,context_images=contexts,scroll_targets=payload) as (_,mappings):
            points = [{'option':'first','click_x':60,'click_y':67},
                      {'option':'last','click_x':60,'click_y':785}]
            decision = {'action':'select','selection_type':'multiple_answer','selections':points}
            result = ground_capture_decision(self.path,decision,mappings)
            self.assertEqual([p['target_view'] for p in result['selections']], ['view-a','view-b'])
            self.assertEqual([p['click_y'] for p in result['selections']], [34,452])
            decision['selections'][1].update(click_x=60,click_y=215)
            with self.assertRaises(AIError): ground_capture_decision(self.path,decision,mappings)

    def test_side_by_side_context_crops_keep_both_offsets(self):
        control = {'kind':'checkbox', 'box':[49,24,71,46]}
        payload = {'controls':{'01':control | {'target_view':'left'},
                               '02':control | {'target_view':'right'}}, 'live_controls':{}}
        contexts = [{'path':self.path, 'box':[10,20,110,100], 'controls':{'01':control}},
                    {'path':self.path, 'box':[10,20,110,100], 'controls':{'02':control}}]
        with coordinate_capture(self.path, context_images=contexts, scroll_targets=payload) as (_, mappings):
            decision = {'action':'select', 'selection_type':'multiple_answer', 'selections':[
                {'option':'left', 'click_x':50, 'click_y':47},
                {'option':'right', 'click_x':162, 'click_y':47}]}
            result = ground_capture_decision(self.path, decision, mappings)
            self.assertEqual([p['target_view'] for p in result['selections']], ['left','right'])
            self.assertEqual([(p['click_x'],p['click_y']) for p in result['selections']], [(60,34),(60,34)])

    def test_normalized_point_in_small_control_on_maximum_height_sheet(self):
        raw = {'action':'select', 'selection_type':'multiple_answer', 'coordinate_space':'normalized_1000',
               'confidence':.99, 'explanation':'Small native checkbox.', 'selections':[
                   {'option':'A', 'click_x':192, 'click_y':977}]}
        parsed = DecisionClient().parse_coordinates(json.dumps(raw), 500, 8192)
        mappings = [{'upload_box':[90,7995,102,8007], 'target_id':'01',
                     'control':{'kind':'checkbox', 'box':[90,95,102,107], 'target_view':'bottom'}}]
        result = ground_capture_decision(self.path, parsed, mappings)
        self.assertEqual(result['selections'][0]['target_view'], 'bottom')
        self.assertEqual((result['selections'][0]['click_x'],result['selections'][0]['click_y']), (96,100))
