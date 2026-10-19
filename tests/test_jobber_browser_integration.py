"""Opt-in real Chromium checks: TELOS_BROWSER_SMOKE=1 python -m unittest discover -s tests -p test_jobber_browser_integration.py."""

import io
import os
import unittest
import zipfile

import pymupdf

from telos.browser import BrowserSession


@unittest.skipUnless(os.getenv("TELOS_BROWSER_SMOKE") == "1", "requires installed patchright Chromium")
class BrowserIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.browser = BrowserSession()
        with pymupdf.open() as doc:
            doc.new_page().insert_text((50, 50), "ESSENTIAL: London hybrid, minimum ten years")
            self.pdf = doc.tobytes()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            archive.writestr("content.xml", (
                '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                '<office:body><office:text><text:p>' + 'Directory country. ' * 3000
                + 'DIRECTORY_TAIL</text:p></office:text></office:body></office:document-content>'
            ))
        self.odt = buffer.getvalue()
        await self.browser._ensure_started()
        await self.browser._context.route("https://jobber-test.invalid/**", self.serve)

    async def asyncTearDown(self):
        await self.browser.close()

    async def serve(self, route):
        path = route.request.url.rsplit("/", 1)[-1]
        if path == "role.pdf":
            await route.fulfill(content_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="role.pdf"'}, body=self.pdf)
        elif path == "directory.odt":
            await route.fulfill(content_type="application/vnd.oasis.opendocument.text",
                                headers={"Content-Disposition": 'attachment; filename="directory.odt"'}, body=self.odt)
        elif path == "blocked":
            await route.fulfill(status=403, body="Access denied")
        elif path == "child":
            await route.fulfill(content_type="text/html", body='<h1>CHILD_DOCUMENT</h1><a href="/grandchild">Next job</a>')
        elif path == "grandchild":
            await route.fulfill(content_type="text/html", body="<h1>GRANDCHILD_DOCUMENT</h1>")
        elif path == "long":
            await route.fulfill(content_type="text/html", body="<main>" + "<p>General requirements.</p>" * 900 + "<p>ESSENTIAL_TAIL</p></main>")
        else:
            await route.fulfill(content_type="text/html", body='''
                <h1>ORIGINAL_PAGE</h1>
                <button style="display:none">Necessary Only</button>
                <button onclick="document.querySelector('h1').textContent='VISIBLE_CLICKED'">Necessary Only</button>
                <button style="display:none">Hidden Control</button>
                <button style="opacity:0">Transparent Control</button>
                <div inert><button>Inert Control</button></div>
                <label for="q">Query</label><input id="q">
                <a href="/child" target="_blank">Job pack</a>
                <a href="/role.pdf">Download role</a>
                <a href="/role.pdf" target="_blank">PDF new tab</a>
                <a href="/directory.odt">Download directory</a>
                <a href="/directory.odt" target="_blank">ODT new tab</a>
                <a href="/blocked">Blocked job</a>
            ''')

    async def test_visible_controls_fill_popup_and_back(self):
        opened = await self.browser.open("https://jobber-test.invalid/start")
        self.assertTrue(opened["ok"], opened)
        self.assertNotIn("Hidden Control", [item["text"] for item in opened["interactive"]])
        self.assertIsNone(await self.browser._resolve_clickable("Hidden Control"))
        self.assertIsNone(await self.browser._resolve_clickable("Transparent Control"))
        self.assertIsNone(await self.browser._resolve_clickable("Inert Control"))
        self.assertIn("VISIBLE_CLICKED", (await self.browser.click("Necessary Only"))["content"])
        self.assertTrue((await self.browser.fill("Query", "London"))["ok"])
        self.assertEqual(await self.browser._page.locator("#q").input_value(), "London")
        child = await self.browser.click("Job pack")
        self.assertTrue(child["ok"], child)
        self.assertIn("CHILD_DOCUMENT", child["content"])
        await self.browser._page.evaluate("history.pushState({}, '', '#section')")
        self.assertIn("CHILD_DOCUMENT", (await self.browser.back())["content"])
        self.assertEqual(len(self.browser._context.pages), 2)
        self.assertIn("GRANDCHILD_DOCUMENT", (await self.browser.click("Next job"))["content"])
        self.assertIn("CHILD_DOCUMENT", (await self.browser.back())["content"])
        self.assertIn("VISIBLE_CLICKED", (await self.browser.back())["content"])

    async def test_pdf_open_click_and_new_tab(self):
        for target in ("Download role", "PDF new tab", None):
            await self.browser.open("https://jobber-test.invalid/start")
            result = await self.browser.click(target) if target else await self.browser.open("https://jobber-test.invalid/role.pdf")
            self.assertTrue(result["ok"], result)
            self.assertIn("minimum ten years", result["content"])
            self.assertIn("[Page 1]", result["content"])
            self.assertIn("minimum ten years", (await self.browser.read())["content"])
            self.assertIn("ORIGINAL_PAGE", (await self.browser.back())["content"])
            self.assertEqual(len(self.browser._context.pages), 1)

    async def test_odt_open_click_new_tab_and_continuation(self):
        for target in ("Download directory", "ODT new tab", None):
            await self.browser.open("https://jobber-test.invalid/start")
            result = await self.browser.click(target) if target else await self.browser.open("https://jobber-test.invalid/directory.odt")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["title"], "directory.odt")
            self.assertEqual(result["url"], "https://jobber-test.invalid/directory.odt")
            self.assertIsNotNone(result["pagination"]["next_offset"])
            text = result["content"]
            while result["pagination"]["next_offset"] is not None:
                result = await self.browser.read(result["pagination"]["next_offset"])
                self.assertTrue(result["ok"], result)
                text += result["content"]
            self.assertIn("DIRECTORY_TAIL", text)
            self.assertIn("ORIGINAL_PAGE", (await self.browser.back())["content"])
            self.assertEqual(len(self.browser._context.pages), 1)

    async def test_pagination_and_blocked_navigation(self):
        result = await self.browser.open("https://jobber-test.invalid/long")
        text = result["content"]
        while result["pagination"]["next_offset"] is not None:
            result = await self.browser.read(result["pagination"]["next_offset"])
            self.assertTrue(result["ok"], result)
            text += result["content"]
        self.assertIn("ESSENTIAL_TAIL", text)
        await self.browser.open("https://jobber-test.invalid/start")
        blocked = await self.browser.click("Blocked job")
        self.assertFalse(blocked["ok"], blocked)
        self.assertIn("403", blocked["error"])
        self.assertTrue((await self.browser.open("https://jobber-test.invalid/start"))["ok"])


if __name__ == "__main__":
    unittest.main()
