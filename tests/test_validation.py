"""Run with:  .venv/bin/python -m unittest discover tests -v"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import validation
from validation import check_box_pairs, check_empty_page, ink_ratio

CLASSES = ["Header", "Footer", "Title", "Text", "Table"]


def box(x1, y1, x2, y2, class_id=3):
    return {"class_id": class_id, "x_center": (x1 + x2) / 2, "y_center": (y1 + y2) / 2,
            "width": x2 - x1, "height": y2 - y1}


def page(lines=0, background=255, ink=0, size=(1000, 1400), fmt="PNG"):
    im = Image.new("L", size, background)
    d = ImageDraw.Draw(im)
    for n in range(lines):
        y = 200 + n * 40
        # A "line of text": dashes of ink with gaps, ~12px tall
        for x in range(100, 900, 20):
            d.rectangle([x, y, x + 12, y + 12], fill=ink)
    buf = io.BytesIO()
    im.save(buf, format=fmt)
    return buf.getvalue()


def rules(issues):
    return [i["rule"] for i in issues]


class BoxPairRules(unittest.TestCase):
    def test_clean_page_passes(self):
        boxes = [box(0.1, 0.1, 0.9, 0.2), box(0.1, 0.25, 0.9, 0.5), box(0.1, 0.55, 0.45, 0.9)]
        self.assertEqual(check_box_pairs(boxes, CLASSES), [])

    def test_touching_edges_pass(self):
        # Shared edge, and a 2px-on-1000px sliver of overlap
        boxes = [box(0.1, 0.1, 0.9, 0.2), box(0.1, 0.2, 0.9, 0.3), box(0.1, 0.298, 0.9, 0.4)]
        self.assertEqual(check_box_pairs(boxes, CLASSES), [])

    def test_small_corner_overlap_passes(self):
        # 0.02 x 0.02 shared corner = 4% of the smaller box
        boxes = [box(0.1, 0.1, 0.3, 0.2), box(0.28, 0.18, 0.48, 0.28)]
        self.assertEqual(check_box_pairs(boxes, CLASSES), [])

    def test_partial_overlap_flagged(self):
        issues = check_box_pairs([box(0.1, 0.1, 0.6, 0.3), box(0.4, 0.2, 0.9, 0.4)], CLASSES)
        self.assertEqual(rules(issues), ["overlap"])
        self.assertEqual(issues[0]["box_indexes"], [0, 1])
        self.assertIn("Overlap", issues[0]["message"])

    def test_nested_box_flagged(self):
        issues = check_box_pairs([box(0.1, 0.1, 0.9, 0.9, 4), box(0.2, 0.2, 0.4, 0.3, 3)], CLASSES)
        self.assertEqual(rules(issues), ["overlap"])
        self.assertIn("Nested box: #2 Text is inside #1 Table", issues[0]["message"])

    def test_duplicate_same_class(self):
        issues = check_box_pairs([box(0.1, 0.1, 0.5, 0.3), box(0.101, 0.101, 0.5, 0.3)], CLASSES)
        self.assertEqual(rules(issues), ["duplicate"])
        self.assertIn("Duplicate box", issues[0]["message"])

    def test_duplicate_different_class_is_conflict(self):
        issues = check_box_pairs([box(0.1, 0.1, 0.5, 0.3, 2), box(0.1, 0.1, 0.5, 0.3, 3)], CLASSES)
        self.assertEqual(rules(issues), ["duplicate"])
        self.assertIn("Conflicting labels: #1 Title and #2 Text", issues[0]["message"])

    def test_pair_reported_once(self):
        # A duplicate is also an overlap — must not be reported twice
        issues = check_box_pairs([box(0.1, 0.1, 0.5, 0.3)] * 2, CLASSES)
        self.assertEqual(len(issues), 1)

    def test_unknown_class_id_does_not_crash(self):
        issues = check_box_pairs([box(0.1, 0.1, 0.5, 0.3, 99)] * 2, CLASSES)
        self.assertIn("Class 99", issues[0]["message"])

    def test_malformed_box_raises(self):
        with self.assertRaises(ValueError):
            check_box_pairs([{"class_id": 1, "x_center": "abc"}], CLASSES)

    def test_issue_cap(self):
        issues = check_box_pairs([box(0.1, 0.1, 0.5, 0.3)] * 30, CLASSES)
        self.assertEqual(len(issues), validation.MAX_ISSUES)


class EmptyPageRule(unittest.TestCase):
    def test_blank_white_page_passes(self):
        self.assertEqual(check_empty_page([], lambda: page()), [])

    def test_blank_grey_scan_passes(self):
        self.assertEqual(check_empty_page([], lambda: page(background=205, fmt="JPEG")), [])

    def test_blank_dark_slide_passes(self):
        self.assertEqual(check_empty_page([], lambda: page(background=20)), [])

    def test_page_with_text_flagged(self):
        self.assertEqual(rules(check_empty_page([], lambda: page(lines=5))), ["empty_page"])

    def test_single_line_flagged(self):
        self.assertEqual(rules(check_empty_page([], lambda: page(lines=1))), ["empty_page"])

    def test_light_text_on_dark_slide_flagged(self):
        issues = check_empty_page([], lambda: page(lines=5, background=20, ink=240))
        self.assertEqual(rules(issues), ["empty_page"])

    def test_scan_shadow_at_page_edge_ignored(self):
        im = Image.new("L", (1000, 1400), 235)
        ImageDraw.Draw(im).rectangle([0, 0, 12, 1400], fill=40)   # dark binding shadow
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        self.assertEqual(check_empty_page([], buf.getvalue), [])

    def test_transparent_png_is_not_ink(self):
        buf = io.BytesIO()
        Image.new("RGBA", (800, 1000), (0, 0, 0, 0)).save(buf, format="PNG")
        self.assertEqual(ink_ratio(buf.getvalue()), 0.0)

    def test_image_not_loaded_when_boxes_exist(self):
        loader = mock.Mock()
        self.assertEqual(check_empty_page([box(0.1, 0.1, 0.5, 0.3)], loader), [])
        loader.assert_not_called()

    def test_unreadable_image_blocks(self):
        def boom():
            raise IOError("s3 down")
        issues = check_empty_page([], boom)
        self.assertEqual(rules(issues), ["empty_page"])
        self.assertIn("Could not load", issues[0]["message"])


class ApproveRoute(unittest.TestCase):
    """End-to-end through /api/review: a failing page must not be saved or approved."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        # Must be set before app import so a real DATABASE_URL in .env is never used.
        os.environ["DATABASE_URL"] = f"sqlite:///{cls.tmp.name}/test.db"
        os.environ["USER_CONFIG"] = ""
        os.environ["AWS_S3_BUCKET"] = "test-bucket"
        from app import create_app
        from models import db, User, WorkItem, Annotation
        cls.db, cls.WorkItem, cls.Annotation = db, WorkItem, Annotation
        cls.app = create_app()
        assert "test.db" in cls.app.config["SQLALCHEMY_DATABASE_URI"]
        with cls.app.app_context():
            jr = User(username="jr_validation_test", role="junior_oa")
            jr.set_password("x")
            db.session.add(jr)
            db.session.commit()
            cls.jr_id = jr.id

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls.db.session.remove()
            cls.db.engine.dispose()
        cls.tmp.cleanup()

    def setUp(self):
        with self.app.app_context():
            self.Annotation.query.delete()
            self.WorkItem.query.delete()
            item = self.WorkItem(s3_key="docs/p1.png", filename="p1.png",
                                 oa_id=self.jr_id, status="annotated")
            self.db.session.add(item)
            self.db.session.add(self.Annotation(s3_key="docs/p1.png", class_id=3, x_center=0.5,
                                                y_center=0.5, width=0.2, height=0.2))
            self.db.session.commit()
            self.item_id = item.id
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.jr_id)
            sess["_fresh"] = True

    def approve(self, annotations):
        return self.client.post(f"/api/review/{self.item_id}",
                                json={"action": "approve", "annotations": annotations})

    def state(self):
        with self.app.app_context():
            item = self.db.session.get(self.WorkItem, self.item_id)
            return item.status, self.Annotation.query.filter_by(s3_key="docs/p1.png").count()

    def test_overlap_blocks_and_saves_nothing(self):
        resp = self.approve([box(0.1, 0.1, 0.6, 0.3), box(0.4, 0.2, 0.9, 0.4)])
        self.assertEqual(resp.status_code, 422)
        body = resp.get_json()
        self.assertEqual(body["status"], "validation_failed")
        self.assertEqual(body["issues"][0]["box_indexes"], [0, 1])
        self.assertEqual(self.state(), ("annotated", 1))   # untouched

    def test_clean_page_approves(self):
        resp = self.approve([box(0.1, 0.1, 0.9, 0.2), box(0.1, 0.3, 0.9, 0.5)])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.state(), ("junior_approved", 2))

    def test_empty_boxes_on_page_with_content_blocks(self):
        with mock.patch("routes_api.get_object_bytes", return_value=page(lines=5)) as s3:
            resp = self.approve([])
        s3.assert_called_once_with("test-bucket", "docs/p1.png")
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.get_json()["issues"][0]["rule"], "empty_page")
        self.assertEqual(self.state(), ("annotated", 1))

    def test_empty_boxes_on_blank_page_approves(self):
        with mock.patch("routes_api.get_object_bytes", return_value=page()):
            resp = self.approve([])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.state(), ("junior_approved", 0))

    def test_malformed_box_is_400(self):
        resp = self.approve([{"class_id": 3}])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.state(), ("annotated", 1))

    def test_reject_is_not_validated(self):
        resp = self.client.post(f"/api/review/{self.item_id}", json={"action": "reject"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.state()[0], "rejected")


if __name__ == "__main__":
    unittest.main()
