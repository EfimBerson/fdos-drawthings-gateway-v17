#!/usr/bin/env python3
"""Offline integration tests: fake CLI binary, real HTTP gateway and file persistence."""
import base64
import contextlib
import importlib.util
import json
import os
import pathlib
import socketserver
import tempfile
import threading
import urllib.request
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).parent
spec = importlib.util.spec_from_file_location('fdos17', HERE / 'gateway.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CLI_OUTPUT = '''Models directory: /tmp/Models
MODEL                               NAME                                 SOURCE    DOWNLOADED  HUGGING_FACE
----------------------------------  -----------------------------------  --------  ----------  ------------
ltx_2.3_22b_distilled_1.1_q6p.ckpt  LTX-2.3 22B [distilled] 1.1 (6-bit)  official  yes         -
z_image_turbo_1.0_i8x.ckpt           Z Image Turbo 1.0 (8-bit S)          official  yes         Tongyi-MAI/Z-Image-Turbo
flux_3_klein_i8x.ckpt                FLUX.3 [klein] 5.6B (8-bit S)        official  yes         black-forest-labs/flux-3-image-klein
krea_2_turbo_i8x.ckpt                Krea 2 Turbo (8-bit S)               official  yes         krea/Krea-2-Turbo
'''

# Tiny valid PNG: 1x1 RGBA
PNG_B64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=='

FAKE_CLI = r'''#!/usr/bin/env python3
import os, sys, pathlib, json, base64
args = sys.argv[1:]
if args[:3] == ['models','list','--downloaded-only']:
    print(os.environ['MOCK_MODELS_TEXT'])
    sys.exit(0)
if args[0] == 'generate':
    assert '--local' in args and '--offline' in args and '--no-download-missing' in args
    output = args[args.index('--output')+1]
    with open(os.environ['MOCK_LOG'], 'a') as f: f.write(json.dumps(args)+'\n')
    pathlib.Path(output).write_bytes(base64.b64decode(os.environ['MOCK_PNG']))
    sys.exit(0)
raise SystemExit(2)
'''

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = pathlib.Path(self.tmp.name)
        self.bin = p/'draw-things-cli'
        self.bin.write_text(FAKE_CLI)
        self.bin.chmod(0o755)
        self.log = p/'cli.log'
        self.old_env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.old_env)))
        os.environ.update({'FDOS_DRAW_THINGS_CLI': str(self.bin), 'MOCK_MODELS_TEXT': CLI_OUTPUT,
                           'MOCK_LOG':str(self.log),'MOCK_PNG':PNG_B64})
        mod._MODEL_CACHE.update({'at':0,'items':[]})
        self.old_save = mod.CONFIG['save_directory']
        mod.CONFIG['save_directory'] = str(p/'saved')
        self.addCleanup(lambda: mod.CONFIG.__setitem__('save_directory',self.old_save))

    def test_catalog(self):
        models = mod.available_models(force=True)
        self.assertEqual([x['id'] for x in models], ['z_image_turbo_1.0_i8x.ckpt','flux_3_klein_i8x.ckpt','krea_2_turbo_i8x.ckpt'])
        published = mod.advertised_models()
        self.assertEqual(len([x for x in published if x['id'].startswith(mod.PROFILE_PREFIX) and '--edit' not in x['id']]),9)
        self.assertFalse(any('ltx_' in x['id'] for x in published))

    def test_cli_generates_and_references(self):
        ref = PNG_B64
        out = mod.run_draw_things_cli('flux_3_klein_i8x.ckpt', 'test',576,1024,references=[ref,ref,ref],crop_canvas=True)
        self.assertEqual(out,[PNG_B64])
        line = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual(line.count('--image'),3)
        self.assertIn('--crop',line)
        self.assertEqual(line[line.index('--model')+1], 'flux_3_klein_i8x.ckpt')

    def test_http_paint_and_edit(self):
        httpd = mod.ThreadingHTTPServer(('127.0.0.1',0),mod.PanelHandler)
        th = threading.Thread(target=httpd.serve_forever,daemon=True)
        th.start()
        self.addCleanup(lambda: (httpd.shutdown(),httpd.server_close(),th.join(timeout=5)))
        base = f'http://127.0.0.1:{httpd.server_port}'
        def post(path, obj):
            data=json.dumps(obj).encode()
            request = urllib.request.Request(base+path,data=data,headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request,timeout=15) as resp:
                return json.load(resp)
        with urllib.request.urlopen(base+'/health') as resp:
            self.assertEqual(json.load(resp)['backend'],'draw-things-cli')
        models=json.load(urllib.request.urlopen(base+'/v1/models'))['data']
        self.assertTrue(any(x['id'].endswith('--2k') for x in models))
        gen=post('/v1/images/generations',{'model':'gpt-image-1@fdos--krea_2_turbo_i8x.ckpt--1k','prompt':'Apple','size':'1536x1024'})
        self.assertEqual(gen['data'][0]['b64_json'],PNG_B64)
        edit=post('/v1/images/edits',{'model':'gpt-image-1@fdos--flux_3_klein_i8x.ckpt--1k--edit','prompt':'Change clothing','size':'1024x1536','images':[PNG_B64,PNG_B64,PNG_B64]})
        self.assertEqual(edit['data'][0]['b64_json'],PNG_B64)
        self.assertEqual(len(list((pathlib.Path(self.tmp.name)/'saved').glob('*.png'))),2)
        lines=[json.loads(x) for x in self.log.read_text().splitlines()]
        self.assertEqual(lines[0][lines[0].index('--model')+1],'krea_2_turbo_i8x.ckpt')
        self.assertEqual(lines[1].count('--image'),3)
        # Standard OpenAI multipart form-data with one image.
        boundary='----fdos-test-boundary'
        payload=(('--'+boundary+'\r\nContent-Disposition: form-data; name="model"\r\n\r\n'
                  'gpt-image-1@fdos--flux_3_klein_i8x.ckpt--1k--edit\r\n'
                  '--'+boundary+'\r\nContent-Disposition: form-data; name="prompt"\r\n\r\n'
                  'Replace outfit\r\n'
                  '--'+boundary+'\r\nContent-Disposition: form-data; name="image"; filename="ref.png"\r\n'
                  'Content-Type: image/png\r\n\r\n').encode()
                 +base64.b64decode(PNG_B64)+('\r\n--'+boundary+'--\r\n').encode())
        req=urllib.request.Request(base+'/v1/images/edits',data=payload,headers={
            'Content-Type':'multipart/form-data; boundary='+boundary})
        with urllib.request.urlopen(req,timeout=15) as resp:
            self.assertEqual(json.load(resp)['data'][0]['b64_json'],PNG_B64)
        # Chat image profile is experimental in Cherry, but HTTP route must work.
        chat=post('/v1/chat/completions',{'model':'fdos-chat--flux_3_klein_i8x.ckpt--1k',
             'messages':[{'role':'user','content':'Generate an apple, aspect 1:1'}], 'stream':False})
        self.assertIn('/generated/',chat['choices'][0]['message']['content'])
        self.assertEqual(len(list((pathlib.Path(self.tmp.name)/'saved').glob('*.png'))),4)

if __name__=='__main__': unittest.main(verbosity=2)
