import unittest
from src.monitor import parse_feed

class FeedParserTests(unittest.TestCase):
    def test_rss_item(self):
        xml = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>New BIM tool</title><link>/news/bim</link><guid>item-1</guid><pubDate>Tue, 25 Nov 2026 08:00:00 GMT</pubDate><description><![CDATA[<p>AI for <b>design</b></p>]]></description></item></channel></rss>"""
        items = parse_feed(xml, "https://example.com/feed.xml")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://example.com/news/bim")
        self.assertEqual(items[0]["title"], "New BIM tool")
        self.assertIn("AI for design", items[0]["summary"])

    def test_atom_entry(self):
        xml = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>tag:example.com,2026:1</id><title>IFC update</title><link href="https://example.com/ifc" rel="alternate"/><updated>2026-11-25T08:00:00Z</updated><summary>Open BIM news</summary></entry></feed>"""
        items = parse_feed(xml, "https://example.com/feed")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://example.com/ifc")
        self.assertIn("IFC", items[0]["title"])

    def test_invalid_xml_raises(self):
        with self.assertRaises(Exception):
            parse_feed("<rss>", "https://example.com/feed")

if __name__ == "__main__":
    unittest.main()
