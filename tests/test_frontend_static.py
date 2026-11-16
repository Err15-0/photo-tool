"""
Static checks of templates/index.html that need no browser.

    python -m unittest discover -s tests -v
"""

import os
import unittest
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VOID_TAGS = {"input", "img", "br", "hr", "meta", "link", "source", "area", "base", "col", "embed", "wbr", "path",
             "rect", "circle", "stop"}


class AncestryParser(HTMLParser):
    """Records the id chain of open elements for every element that has an id."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.ancestors = {}

    def handle_starttag(self, tag, attrs):
        element_id = dict(attrs).get("id")
        if element_id:
            self.ancestors[element_id] = [i for _t, i in self.stack if i]
        if tag not in VOID_TAGS:
            self.stack.append((tag, element_id))

    def handle_startendtag(self, tag, attrs):
        element_id = dict(attrs).get("id")
        if element_id:
            self.ancestors[element_id] = [i for _t, i in self.stack if i]

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class UploadInputsTests(unittest.TestCase):
    """
    Regression: the hidden file inputs used to sit INSIDE #drop-zone. Clicking "Add Folder"
    triggered input-folder.click(), the click bubbled to #drop-zone, whose handler opened
    input-files – so the browser showed the normal file picker instead of the folder picker.
    """

    def setUp(self):
        parser = AncestryParser()
        with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as handle:
            parser.feed(handle.read())
        self.ancestors = parser.ancestors

    def test_inputs_exist_and_folder_input_is_a_directory_picker(self):
        with open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('id="input-folder" type="file" multiple webkitdirectory', html)
        self.assertIn("input-files", self.ancestors)
        self.assertIn("input-folder", self.ancestors)

    def test_file_inputs_are_not_inside_the_drop_zone(self):
        self.assertIn("drop-zone", self.ancestors)
        for input_id in ("input-files", "input-folder"):
            self.assertNotIn("drop-zone", self.ancestors[input_id],
                             f"#{input_id} must not be inside #drop-zone (its clicks would bubble)")

    def test_drop_zone_click_handler_ignores_bubbled_clicks(self):
        with open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn('dropZone.addEventListener("click", () => $("input-files").click());', source)
        self.assertIn('if (e.target.closest("button, input, a, label")) return;', source)


if __name__ == "__main__":
    unittest.main()
