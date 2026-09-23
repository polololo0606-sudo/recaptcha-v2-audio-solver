"""Opt-in local browser check. All HTTP requests are fulfilled with local fixtures."""
import argparse
import contextlib
import importlib.util
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright

spec=importlib.util.spec_from_file_location('image_solver',Path(__file__).resolve().parents[1]/'standalone/captchasolver.py')
solver=importlib.util.module_from_spec(spec)
spec.loader.exec_module(solver)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-path',help='Optional Chrome/Chromium executable')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        kwargs={'headless':True}
        if args.browser_path:kwargs['executable_path']=args.browser_path
        browser=pw.chromium.launch(**kwargs)
        try:
            context=browser.new_context(viewport={'width':1440,'height':960})
            grid='''<style>body{margin:0}.rc-imageselect-table-44{position:absolute;left:20px;top:30px;width:400px;height:400px;display:grid;grid-template-columns:repeat(4,1fr)}button{border:0;padding:0}</style><div class="rc-imageselect-desc-wrapper">Select all squares with buses</div><div class="rc-imageselect-table-44">'''+''.join('<button style="background:rgb(255,0,0)" onclick="document.body.dataset.clicked=\''+str(i)+'\'">'+str(i)+'</button>' for i in range(1,17))+'</div>'
            main_page='''<style>body{margin:0;height:2400px}iframe{position:absolute;left:100px;top:150px;width:500px;height:500px;border:0}</style><iframe src="https://fixture.invalid/recaptcha/api2/bframe"></iframe>'''
            context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=grid if 'bframe' in route.request.url else main_page))
            page=context.new_page();page.goto('https://fixture.invalid')
            page.locator('iframe').content_frame.locator('button').first.wait_for()
            for name in ('OUTPUT_DIR','FULL_DIR','GRID_DIR','OVERLAY_DIR','TILES_DIR','REPORTS_DIR'):
                setattr(solver,name,Path(tmp)/name.lower())
            solver.ensure_dirs()
            for scrolled in (False,True):
                if scrolled:page.evaluate("document.querySelector('iframe').style.top='1200px'; window.scrollTo(0,1000)")
                frame,frame_box=solver.get_challenge_frame(page)
                size,element,grid_box=solver.detect_grid(frame)
                for tile in range(1,17):
                    with patch.object(solver.time,'sleep'),patch.object(solver.random,'uniform',return_value=0),contextlib.redirect_stdout(io.StringIO()):
                        solver.click_tiles(page,[tile],size,grid_box,frame_box)
                    actual=page.locator('iframe').content_frame.locator('body').get_attribute('data-clicked')
                    assert actual==str(tile),(tile,actual,scrolled)
                with patch.object(solver,'lm_full_grid',return_value=([],{},{})),patch.object(solver,'click_skip',return_value=True),patch.object(solver,'pw_wait'),contextlib.redirect_stdout(io.StringIO()):
                    result=solver.process_slot(page,'fixture',int(scrolled)+1)
                with Image.open(result['grid_path']) as image:
                    assert image.size==(400,400),image.size
                    assert image.convert('RGB').getpixel((325,325))==(255,0,0), image.convert('RGB').getpixel((325,325))
                print('PASS: all 16 tile clicks and exact grid capture; scrolled='+str(scrolled))
        finally:browser.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
