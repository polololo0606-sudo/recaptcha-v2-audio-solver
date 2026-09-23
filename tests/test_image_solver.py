import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

spec=importlib.util.spec_from_file_location('image_solver',Path(__file__).resolve().parents[1]/'standalone/captchasolver.py')
solver=importlib.util.module_from_spec(spec)
spec.loader.exec_module(solver)


class ImageSolverTests(unittest.TestCase):
    def test_iframe_offset_is_not_added_twice(self):
        grid={'x':120,'y':180,'width':400,'height':400}
        frame={'x':100,'y':150,'width':500,'height':500}
        self.assertEqual(solver.tile_page_xy(1,4,grid,frame),(170,230))
        self.assertEqual(solver.tile_page_xy(16,4,grid,frame),(470,530))

    def test_non_object_model_json_is_rejected(self):
        for value in ('[1,2]','true','42','"text"','null'):
            with self.subTest(value=value):self.assertIsNone(solver._parse_json(value))
        self.assertEqual(solver._parse_json('```json\n{"yes":false}\n```'),{'yes':False})

    def test_only_true_boolean_selects_tile(self):
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/'tile.png';Image.new('RGB',(100,100),'red').save(image)
            for raw,expected in [('{"yes":false}',False),('{"yes":"false"}',False),('{"yes":"true"}',False),('{"yes":true}',True),('[1,2]',False)]:
                with self.subTest(raw=raw),patch.object(solver,'_lm',return_value=raw):
                    self.assertIs(solver.lm_classify_tile(image,'bus')[0],expected)

    def test_invalid_confidence_requests_recheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/'tile.png';Image.new('RGB',(100,100),'red').save(image)
            for value in ('null','"unknown"','"NaN"','"Infinity"'):
                with self.subTest(value=value),patch.object(solver,'_lm',return_value='{"yes":true,"confidence":'+value+'}'):
                    self.assertEqual(solver.lm_classify_tile(image,'bus')[1],0)

    def test_tiles_reconstruct_full_grid(self):
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/'grid.png';Image.new('RGB',(400,400),'red').save(image)
            tiles=solver.split_tiles(image,4,Path(tmp)/'tiles')
            self.assertEqual([t[0] for t in tiles],list(range(1,17)))
            for tile in tiles:
                with Image.open(tile[3]) as opened:
                    self.assertEqual(opened.size,(100,100))


if __name__ == '__main__':unittest.main()
